---
status: accepted
date: 2026-09-27
---

# Configuration Changes return a typed outcome

## Context

Two pipelines write a Server Configuration. `KeaClient._persist_config` follows a native command such as
`subnet4-delta-add` or `network4-add`. `KeaClient._apply_config` pushes a read-modify-write result with
`config-set`. The two pipelines report failure through four exception classes: `KeaConfigTestError`,
`KeaConfigPersistError`, `PartialPersistError` and `AmbiguousConfigSetError`.

Five view modules hold 39 `except` clauses that translate those classes and `KeaException` to messages by hand.
The handlers disagree:

- Only two handlers give `AmbiguousConfigSetError` its own message. The others say that the change "may not
  survive a restart", which is wrong for an unconfirmed change.
- A native command has no unconfirmed state. A lost reply on `network_add` reads "An internal error occurred".
- A view runs the add, delete and compensating rollback of a Shared Network move, and persists after each step.

ADR 0002 already gives Reservation mutations a typed result. ADR 0003 left the configuration mutation interface
as future work.

## Decision

Create a `config_write` module. It owns every Configuration Change to a Server Configuration.

### Interface

Purpose-named operations. Each takes `(server, family)` and typed arguments, and builds its own `KeaClient`:

```text
add_subnet, edit_subnet, delete_subnet
add_pool, delete_pool
add_shared_network, edit_shared_network, delete_shared_network
set_subnet_options, set_server_options
add_option_definition, delete_option_definition
```

An operation returns a Configuration Change Outcome when the change is live or can be live:

```text
application: "applied" | "unknown"
persistence: "persisted" | "failed" | "not-requested"
diagnostics: tuple[str, ...]
```

`add_subnet` also returns the assigned Subnet ID.

An operation raises one rejection exception when the change is certainly not live. The exception carries a
reason: Kea rejected the command, `config-test` rejected the candidate configuration, the request was never
sent, or the client configuration is invalid.

### Application

- Only connect-phase failures mean that the request was never sent: `requests.ConnectTimeout`, and a
  `requests.ConnectionError` caused by urllib3 `NewConnectionError`. The session sends one POST and does not
  retry.
- A read timeout, a reset during the reply, or a malformed reply to a command that can change the configuration
  gives `unknown`. The same failure on a read, or on the `config-test` of a candidate configuration, comes before
  any such command. It is a rejection because the change was never sent.
- No operation probes Kea to resolve `unknown`. A concurrent writer would make the probe lie: an observed
  state proves what Kea holds now, not which request wrote it.

### Subnet and Pool changes

`add_subnet`, `edit_subnet`, `delete_subnet`, `add_pool`, `delete_pool` and `set_subnet_options` open the Subnet
Catalogue `MutationScope` themselves. Views do not touch the scope for these writes. This completes the Subnet
mutation rules of ADR 0001.

- Subnet creation already follows ADR 0001: the view gets the identity from `MutationScope.prepare_creation`,
  and a rejected allocated ID causes one retry only when a fresh scope shows that ID is now taken. `add_subnet`
  takes over that logic from the view. It never reads Kea's error text, and an ID from the operator never
  causes a retry.
- After a lost reply, `add_subnet` returns `unknown` with the ID it sent, and runs no dependent step. A later
  Verified Subnet with the same CIDR and ID does not prove that this request created it, because another writer
  can create the same identity after `prepare_creation`. The lookup by CIDR in `KeaClient.subnet_add` is
  deleted.
- `edit_subnet`, `delete_subnet` and `set_subnet_options` take the Subnet ID and the CIDR that the operator saw.
  The scope must return a Verified Subnet with that ID and that CIDR. Otherwise the operation raises a rejection
  that tells the operator to reload. An ID that another Subnet reuses between the form and the check therefore
  cannot redirect the change. A reuse between the check and the Kea write stays possible, because Kea has no
  conditional write.
- `add_pool` and `delete_pool` take the same ID and CIDR pair, and a typed `Pool`. The Verified Subnet supplies
  the CIDR for the delta commands, so the separate `subnet-get` lookup goes.

One Pool parser exists: the one in `server_configuration`. The forms use it. A Pool outside its Subnet, or a Pool
that overlaps an existing Pool of the Verified Subnet, is a form error. When the configuration facts of the
Subnet are missing, the overlap check does not run, and Kea decides. A Reservation address inside a Pool stays a
warning, and one domain function computes it for both the Pool and the Reservation forms.

### Persistence

- `KeaClient.persist(family)` is the one persist step: `config-get`, `config-test` on the live configuration,
  then `config-write`. It returns `persisted`, `failed` or `not-requested`. It does not raise.
- `failed` means that NetBox cannot confirm that the disk copy holds the running configuration, so a restart
  can lose a live change. It covers a `config-write` failure, a lost `config-write` reply, and a `config-test`
  rejection of the live configuration. The diagnostic says which. With `application="unknown"`, the message
  does not claim that the change is live.
- The persist step also runs after an `unknown` application. `config-write` saves what Kea runs, so the disk
  copy then matches memory whether the change applied or not.
- Reservation mutations call the same step. `ReservationMutationResult.persistence` uses the shared type.

### Multi-step changes

A Configuration Change that needs several Kea commands is one operation. Examples: add a Subnet and assign it
to a Shared Network, or move a Subnet between Shared Networks. The module runs the steps and persists once at
the end.

After an `unknown` step, the operation runs no further step and returns `application="unknown"`.

When a later step certainly did not apply, because Kea rejected it or the request was never sent, the module rolls
back the steps that applied, newest first. Before it undoes a step, it reads the target again in a fresh scope. It
undoes the step only when the target still holds the state that this operation wrote. Otherwise another writer
changed the target, so the module leaves it and does not roll back earlier steps either.

- Every undo succeeds: nothing that this operation wrote is live, and the operation raises the rejection.
- An undo fails, or a target changed: the operation returns `application="unknown"` with a diagnostic that names
  each step and its state.

Kea's control API has no conditional write, so a writer outside NetBox can still change a target between the
check and the undo. The check narrows that window. It does not close it.

### Serialization

Every operation holds a transaction-level PostgreSQL advisory lock on `(server, family)` from its first read to
the end of its persist step. Two NetBox operations on one Server and family therefore run one at a time. A
read-modify-write through `config-set` cannot erase the change of another NetBox operation, and no NetBox
operation changes a target between a rollback check and its undo. The wait for the lock is bounded. When it
expires, the operation raises a rejection because the request was never sent.

The lock does not stop Kea administrators or other tools, because they write to Kea directly. A `config-set`
can still erase a change that such a writer made after the read.

### Placement

`KeaClient` keeps single-command primitives, cache invalidation around each command, and `persist`.
`config_write` owns sequencing, rollback, persistence and the outcome. `server_configuration` stays read only.
A read-modify-write operation reads the raw configuration through `KeaClient`, because it must keep Kea fields
that the typed facts do not model.

`KeaConfigTestError`, `KeaConfigPersistError`, `PartialPersistError` and `AmbiguousConfigSetError` are deleted.

### Presentation

One function in `views/_base.py` maps an outcome or a rejection to a message. `_kea_options_mutation` is
deleted. An OpenGrep rule refuses an `except` of the write exceptions in `views/` outside that function.

### Scope

Lease writes, lease wipe, and `dhcp-enable` or `dhcp-disable` change runtime state, not the Server
Configuration. They are not Configuration Changes.

## Consequences

Views lose their exception handlers and the Shared Network move rollback. A new failure mode needs one change
in `config_write` and one row in the message mapper.

Configuration Changes to one Server and family wait for each other. A slow Kea reply delays the next change by
up to the bounded lock wait, and then that change is rejected.

A caller that ignores the returned outcome loses a persistence warning or an `unknown` application. It cannot
mistake a rejection for success, because a rejection raises.

The replacement is completed in one change. Tests cross the `config_write` interface with a real Server and a
real `KeaClient`, and stub only `requests.Session.post`.

## Rejected alternatives

- Return an outcome for every result, including rejection: rejected because a caller that forgets to check
  reads a rejection as success.
- Keep exceptions in one hierarchy with an outcome attribute: rejected because every view keeps a
  `try`/`except`.
- A separate state for a `config-test` rejection of the live configuration: rejected because the operator
  consequence is the same as a `config-write` failure.
- Probe each target after a lost reply, or compare configuration hashes: rejected because a concurrent writer
  makes both probes lie, and each probe adds a read that can fail.
- Skip the persist step after an `unknown` application: rejected because the disk copy then lags a change that
  did go live.
- Put the operations on `KeaClient`: rejected because domain sequencing and rollback do not belong in the
  wire client.
- One entry point that takes a union of typed changes: rejected because the result payload becomes generic,
  and ADR 0001 and 0003 prefer purpose-named operations.
- Let views sequence multi-step changes: rejected because the rollback logic stays in each view.
- No rollback, with a third `partial` application state: rejected because every caller must then handle a
  third state.
- Treat every transport error as `unknown`: rejected because a stopped Kea then reads as "check the server"
  instead of "not applied".
- Let views open `MutationScope` and pass a Verified Subnet: rejected because every Subnet view then manages
  the scope, and the `(server, family)` contract breaks.
- Detect an ID collision from Kea's error text: rejected because a reworded message breaks the retry.
- Report `applied` after a lost reply when a Verified Subnet has the same CIDR and ID: rejected because
  another writer can create that identity after `prepare_creation`, which reserves nothing. The match proves the
  current state, not that this request applied.
- No NetBox lock, because it cannot stop Kea administrators or other tools: rejected because two NetBox
  read-modify-write operations then erase each other's changes. The lock covers NetBox operations, and the check
  before each undo narrows the window for other writers as far as Kea allows.
- Undo a step without checking its target: rejected because the undo can remove a concurrent writer's change.
- Identify a Subnet for edit or delete by ID only: rejected because an ID can be reused by another Subnet
  between the form and the write.
- Send every Pool check to Kea: rejected because a form error names the field. Kea still decides when the
  configuration facts are missing.
