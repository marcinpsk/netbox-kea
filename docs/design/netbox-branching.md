<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# netbox-branching compatibility

This is the design for running netbox-kea with netbox-branching. It applies to NetBox 4.7.0 with
netbox-branching 1.2.1, and to branches created after the release that adds support. ADR 0007
records the decision, and issue #231 tracks the implementation.

## Problem brief

NetBox Branching (netbox-labs/netbox-branching, v1.2.1, NetBox 4.7 only) stages changes in a
per-branch PostgreSQL schema. Its `BranchAwareRouter` (`netbox_branching/database.py`) sends reads
and writes of branchable models to a `schema_<id>` connection while a branch is active. That
connection uses `search_path=<branch>,<main>`, so a table absent from the branch schema resolves
to main. A model is branchable when it is in `INCLUDE_MODELS`, when a registered resolver says so,
or when it inherits `ChangeLoggingMixin`; `EXEMPT_MODELS` and `exempt_models` always win
(`utilities.py` `supports_branching`). Merge, sync and revert replay `ObjectChange` rows only.

netbox-kea has no code for branching today, and a user can install both. Kea is an external
system: every Kea change the plugin makes is live when the command returns. A branch cannot
stage it.

The question: what does the plugin guarantee when netbox-branching is installed, and what code
enforces it?

### Operator decisions

- Support level (2026-09-29): **reads work in a branch; every write is refused.** Rejected: refuse
  every plugin URL (the netbox-data-import design), and full support (the netbox-librenms design).

### Constraints

- netbox-branching stays optional. Without it, behaviour does not change.
- No backwards-compatibility layers; remove obsolete paths.
- Fail fast and visibly; no silent fallback to main.
- The plugin was never compatible with netbox-branching, so no deployment has a branch that
  predates the release that adds support (the netbox-data-import operator decision, applied here).

### Acceptance conditions

1. With a branch active, every plugin entry point (UI view, REST API, GraphQL, background job,
   template extension on a core page) has one documented outcome. A read returns data and says
   where the plugin's own sources come from: Kea data is live, and netbox_kea rows come from main.
   It names the active branch as the routing context only. Other NetBox objects follow
   netbox-branching's routing. A write is refused before any Kea command and before any database
   write. No outcome is an unhandled 500.
2. A NetBox core action executed in a branch does not change the plugin's rows in main, and sends
   no Kea command. A change to a global object (an exempt or non-branchable core model, such as
   CustomField, User, `core.Job`, Bookmark) lands in main, and so do its effects on plugin rows.
3. After a branch merges or is discarded, main's plugin rows are consistent with main's NetBox
   objects.
4. A CI job with netbox-branching on NetBox 4.7 runs tests that fail when 1 to 3 regress.
5. A mechanical guard stops a new plugin route or a new Kea write command from bypassing the
   refusal. Queryset `update()`, `bulk_create()`, `bulk_update()` and raw SQL on plugin models
   are outside the contract (see the table below), so no guard covers them.

### Prior art (sibling plugins, same NetBox and netbox-branching versions)

- netbox-data-import: main only. A resolver makes a plugin model branchable
  only when it has a database foreign key to a branchable model outside the plugin. Middleware
  refuses every plugin URL callback with 409. Jobs and the profile lock refuse. A revert validator
  refuses reverts that cannot restore plugin data. `fake_on_branch = True` on data migrations.
  Guards: a URL-tree walk, a pinned branchable set, a migration check.
- netbox-librenms: full support. Nested transactions on `default` and the branch
  alias (netbox-branching writes `ChangeDiff` on `default`), `lock_timeout` for cycles across the
  two sessions of one request, `snapshot()` before every update, cache keys per alias. Most of its
  defects came from the two-connection transaction model.

### Evidence

Read at d548e1a2, NetBox 4.7.0 (`NB/`), netbox-branching 1.2.1 (`NBB/`), netbox-plugin-dhcp 0.2.0
(`DHCP/`). Counts are by AST over `netbox_kea/` without tests and migrations. Effects marked
*derived* come from source reading, not from a provisioned branch.

Models and routing:

- `Server` (`JobsMixin, NetBoxModel`) is branchable. `sync_vrf` is a `SET_NULL` foreign key to
  `ipam.VRF` with `related_name="+"`. Tags use `extras.TaggedItem`.
- `SyncConfig` (singleton) and `KeaDhcpLink` are plain `models.Model`, so they stay in main.
  `KeaDhcpLink.server` is `CASCADE` to Server; `sys4_object` is a GenericForeignKey to netbox_dhcp
  rows. Every netbox_dhcp model is a `NetBoxModel` or `PrimaryModel`, so all are branchable.
- `Server.get_client()` builds the Kea client from the Server row (`models.py:241-294`), and
  `Server.clean()` sends `version-get` on every create and edit (`models.py:296-339`).
- Only branchable tables are copied into a branch (`NBB/utilities.py:293-317`). Branch tables share
  main's id sequences (`NBB/provisioning.py:346-356`).

Entry points (95 routes, 88 view classes; 44 gated by `_KeaChangeMixin`, `views/_base.py:65-86`):

- Kea writes: lease add, edit, bulk delete and import; subnet wipe; reservation add, edit, delete
  and import; every configuration change through `config_write`; DHCP enable and disable. All are
  POST. The Kea change is live before any follow-up database write (JournalEntry, IPAM sync).
- Database writes: Server CRUD (generic views, REST viewset), `sync-toggle`, `sync-jobs` POST,
  `sync-now` (enqueues `core.Job`), lease and reservation sync to IPAM, the DHCP-plugin import
  (netbox_dhcp objects, IPAM rows, `KeaDhcpLink`).
- No GET handler reaches a Kea write. Two GET handlers write the database: `sync-jobs/` and
  `servers/<pk>/sync_status` call `SyncConfig.get()` (`get_or_create` and a one-time backfill,
  `models.py:452-483`). Display reads write Redis. One POST only reads: lease bulk delete without
  `_confirm` renders the confirmation page (`views/leases.py:534-548`).
- REST: one `NetBoxModelViewSet` with four read-only Kea actions. GraphQL: `server` and
  `server_list` queries. One template extension, a read-only panel on IPAddress.
- Generic bulk views can run as `AsyncViewJob`, which applies request processors, so the branch is
  active in the worker (`NB/netbox/jobs.py:267-273`).

Jobs: `KeaIpamSyncJob` is the only `JobRunner`, a system job every 5 minutes plus `sync-now`.
Workers apply no request processors (`NB/netbox/jobs.py:110-138`), so it always writes main, and
with no request it writes no ObjectChange (`NB/core/signals.py:97-100`). It writes IPAddress,
Prefix, IPRange, MACAddress, `core.Job` and SyncConfig. Branch sync pulls main changes from
ObjectChange only (`NBB/models/branches.py:412-426`), so a branch never sees these writes.

Transactions: 8 `transaction.atomic()` calls, none with `using=`. Seven are in the DHCP-plugin
import (`integrations/dhcp_plugin.py`); they open on `default` while the netbox_dhcp and IPAM
writes inside them route to the branch. One is `config_write._serialized`, which only takes a
`pg_advisory_xact_lock`. No `select_for_update`, `on_commit` or `.using()` in runtime code.

Signals: no receivers in netbox_kea. Five custom signals are sent after a Kea success.

Cascades with a branch active (*derived*):

| Action in the branch | Effect |
|---|---|
| Delete a Server | Branch Server row deleted. `KeaDhcpLink` rows (table not copied) deleted **in main**, with no ObjectChange. `core.Job`, Bookmark and Subscription rows deleted in main |
| Delete the VRF in `Server.sync_vrf` | Branch Server copy nulled, with no ObjectChange (`related_name="+"` hides the relation from NetBox's changelog loop). Main unchanged until merge |
| Delete a netbox_dhcp object a `KeaDhcpLink` points at | No cascade (GenericForeignKey). The main link row points at a deleted id. Same without branching |
| Delete an IPAM object the sync wrote | No plugin table references it (ADR 0006 is accepted, not implemented). netbox_dhcp `PROTECT` keys raise `ProtectedError` for Prefix and IPRange |

Migrations: only 0015 has `RunPython` (deletes invalid `KeaDhcpLink` rows); branch migrate fakes it,
because its only model operation targets a non-branchable model. 0008 is
`SeparateDatabaseAndState` and 0016 alters Server columns; neither is faked.

Caches: Redis keys are `netbox_kea:{server_configuration|subnet_catalogue}:v1:<server.pk>:<family>:...`,
with no branch component. A branch and main share one snapshot per Server, even when the branch
row has different connection fields.

`snapshot()`: no call in netbox_kea. 13 update sites skip it (IPAM sync, DHCP-plugin import,
`sync-toggle`). Server edits through NetBox generic views do take it.

CI: unit tests on NetBox 4.7.0; a DHCP-plugin job with netbox-plugin-dhcp 0.2.0; a black-box
matrix (4.3, 4.7, snapshot). No branching setup anywhere.

ADR 0006 (accepted, not implemented) adds an IPAM ownership link model with `CASCADE` keys to
IPAddress, Prefix and IPRange, advisory locks and `SELECT ... FOR UPDATE`. It does not decide
whether that model is branchable.

### Candidate shapes

Two decisions are open inside the operator's support level.

Where the write refusal lives:

- A. Plugin middleware that refuses unsafe HTTP methods on plugin URLs while a branch is active,
  with named exceptions (the lease delete confirmation POST).
- B. At the sinks: the Kea client refuses mutating commands, and a database write guard refuses
  plugin writes, whatever the entry point.
- C. A per-view declaration (read or write), enforced by a URL-tree test.

Which plugin models are branchable:

- i. Default: Server branchable, `SyncConfig` and `KeaDhcpLink` main-only.
- ii. A resolver with the netbox-data-import rule (branchable only with a database foreign key to
  a branchable model outside the plugin).
- iii. Every plugin model main-only, Server included.

## Decisions

These choices had no competing alternative: a `netbox_kea/branching.py` owner; refusal in plugin
middleware (branch activation runs earlier, in `CoreMiddleware` request processors, and a processor
cannot refuse, `NB/utilities/request.py:131-142`); a read or write kind on every Kea command,
checked in `KeaClient.command()` (`kea.py:727-769`, the single HTTP funnel); `KeaIpamSyncJob` stays
main-only; `SyncConfig` main-only; the ADR 0006 link model main-only; a CI job with a real
provisioned branch.

| # | Decision | Chosen | Rejected | Evidence and reason |
|---|---|---|---|---|
| D1 | Is `Server` branchable | No: resolver `False`. One row per pk, so the Kea client and the Redis keys always use main's current connection fields | Yes. Branch reads use the branch copy's connection fields. Branch sync is refused when main changed Server create, delete or connection fields; the operator recreates the branch | Server changelogs censor passwords (`models.py:341-361`), so a synced connection edit writes a censored password into the branch copy. Sync selects by branchable type (`NBB/models/branches.py:412-426`), so a main-only Server never syncs. With a branchable Server, any main edit of a Server's connection fields blocks sync of every open branch, and the design needs a cache bypass (D6), sync and merge validators (D8), and a stale-copy policy; a main-only Server needs none of them |
| D2 | `Server.sync_vrf` | `SET_NULL` to `PROTECT` | Keep `SET_NULL`; make the reverse relation visible so NetBox snapshots the Server before it nulls the key | With Server main-only, a `SET_NULL` cascade from a branch VRF delete runs on the branch connection and resolves `netbox_kea_server` to main, so main changes at once. `PROTECT` raises in `Collector.collect()` before any write or signal. Today a VRF delete silently moves the next sync into the global VRF. **Operator-visible change in main:** a VRF that a Server syncs into can no longer be deleted until the Server stops using it |
| D3 | `KeaDhcpLink` | Main-only; a link to a deleted target stays until the next import relinks (today's behaviour) | Branchable, plus receivers that delete links when a netbox_dhcp target is deleted | A receiver running in a branch against a main-only table deletes main rows. Branchable links need revert and sync validators (sync writes synthetic DELETEs for them, `branches.py:837-897`). Readers already treat a missing target as absent (`integrations/dhcp_plugin.py:650-654`). The receivers improve main without branching too; they are a follow-up. Reopen if a reader treats a dangling link as a live object |
| D4 | Refusal seams | Middleware (by HTTP method), the Kea transport, `pre_save`/`pre_delete` receivers on the three plugin models, a job guard | Middleware (by classified operation), view dispatch again for `AsyncViewJob`, REST action guards, guards on every domain write, an AST write-boundary gate | Every plugin HTTP entry, including those that enqueue `AsyncViewJob` or `AsyncAPIJob`, passes plugin middleware first. A job can only be queued by a request that the middleware already let through, so a branch-context plugin job cannot exist. A Custom Script reaches plugin write code without plugin middleware (`NB/extras/jobs.py:398-403`); the transport and the receivers refuse its Kea mutations and plugin instance writes, and the contract lists the rest. Reopen on another caller that reaches plugin write code with a branch active and without plugin middleware |
| D5 | Read versus write | HTTP method; no exceptions | A classified inventory of every operation | The one read-only POST (lease bulk delete confirmation, `views/leases.py:534-548`) leads only to a delete that is refused anyway. No GET reaches a Kea write |
| D6 | Redis caches in a branch | Shared; valid because D1 gives one connection per Server | Bypass both caches in a branch | Follows D1 |
| D7 | `SyncConfig.get()` on GET | A data migration creates the singleton and applies the backfill; `get()` becomes a read | A separate read-only lookup for branches; creation stays in guarded main operations | The rejected shape keeps two paths to one row |
| D8 | Sync, merge and revert validators | None: no plugin row exists in a branch | Refuse merge, sync and revert for Server create, delete, connection changes, link changes, and deletes of link-target types | Follows D1 and D3 |
| D9 | `Server.clean()` sends `version-get` | Out of scope, a follow-up | Move the connectivity check out of `clean()` into the form and serializer validation | A Server change record created by a Tag delete in a branch is written to main's changelog: the ObjectChange takes the loaded Server's database through its ContentType foreign key (`NB/netbox/models/features.py:117-127`, `NBB/database.py:49-50`), and NBB records no ChangeDiff for it (`NBB/signal_receivers.py:125-127`). Under D1 no Server change reaches a branch changelog, so no merge replays `Server.clean()` |
| D10 | Kea client used from a thread pool | The client records the branch state at construction; `clone()` keeps it | Context variable only | Thread-pool workers do not inherit context variables; clones run in pools (`kea.py:772-789`, `views/leases.py:944`) |
| D11 | Stale branch selection | Refuse with 409 `branch_selection_unusable` | Run the request on main, as for every NetBox view | A stale cookie or unready query runs on main (`NBB/utilities.py:548-597`, `NBB/middleware.py:53-89`), which the "no silent fallback" constraint forbids |
| D12 | Write controls in the UI | A banner; the IPAddress panel hides its add links | Disable mutation controls | 44 mutation views. Refusal already meets condition 1; disabling controls is a follow-up |
| D13 | Early Server delete guard | `pre_delete` receiver | At `delete()`, because `JobsMixin.delete()` removes jobs before the collector runs | `JobsMixin.delete()` wraps the job deletion and `super().delete()` in one `atomic(using=...)` (`NB/netbox/models/features.py:513-520`), so a `pre_delete` refusal rolls the job deletion back. Reopen if NetBox removes that transaction |
| D14 | Startup validation of `exempt_models` | None; every plugin model remains main-only, including the ADR 0006 ownership link | Reject exemptions that break the required branchable set | No plugin model is branchable today, so no exemption can break the set |
| D15 | Branch table footprint check per request | None | Validate before reads and core actions | Constraint: no branch predates this release. Guard 2 blocks a new branchable model without a design decision |
| D16 | REST refusal code | `branch_write_refused` | `branch_not_supported` | Reads are supported |
| D17 | CI variants | netbox-plugin-dhcp present | Present and absent | Nothing in the design differs with the DHCP plugin absent |

## Design

### Contract

Kea is live and shared by every branch. The plugin's own rows describe that live system, so they
exist in main only. In a branch the plugin is a viewer.

| Entry point, branch active | Outcome |
|---|---|
| Plugin-owned URL callback, GET, HEAD or OPTIONS (UI, HTMX) | Served. A banner on plugin pages says: Kea data is live; Kea servers, sync settings and DHCP plugin links come from main; other NetBox objects follow netbox-branching's routing for branch `<name>`; changes are refused |
| Response from a plugin-owned callback or the GraphQL endpoint while a branch is active | Carries `X-NetBox-Kea-Sources: kea=live; plugin=main; branch=<schema_id>`: Kea data in the response is live, and netbox_kea rows (Server, sync settings, links) come from main. `branch` names the active routing context; it does not state where other NetBox objects came from (netbox-branching's routing decides that, and exempt models such as `core.Job` read main) |
| Plugin-owned URL callback, any other method (UI) | HTTP 409 page naming the branch, with a link to switch to main (`?_branch=`) |
| Plugin-owned URL callback, any other method (HTMX) | A Django error message is queued, and the 409, with an empty body, carries `HX-Refresh: true`. htmx swaps no 4xx response by default (`NB/static/django_htmx/htmx-2.js:264-267`), but handles `HX-Refresh` before its status rules (`htmx-2.js:4831-4843`), so the page reloads in the branch and shows the message. The empty body leaves the message unconsumed for the reload |
| Plugin-owned URL callback, any other method (REST) | HTTP 409, `{"detail": ..., "code": "branch_write_refused"}` |
| GraphQL `server`, `server_list` | Served from main, with the sources header. Nested branchable objects (`sync_vrf`, tags) come from the branch |
| IPAddress panel on a core page | Served; lists main's Servers under a "Kea servers (main)" label; hides the reservation add links |
| `KeaIpamSyncJob` | Always runs on main (workers apply no request processors). Raises `JobFailed` before any read if a branch is active in the worker |
| Non-HTTP caller in a branch (Custom Script, event-rule script, `nbshell`) | A mutating Kea command raises `BranchActive` in the transport. A `save()` or `delete()` of `Server`, `SyncConfig` or `KeaDhcpLink` raises `BranchActive`. Core rows follow branch routing, including rows that plugin helpers write. Queryset `update()`, `bulk_create()`, `bulk_update()` and raw SQL on plugin models are outside the contract, as for every non-branchable model in NetBox |
| Core metadata on a Server (journal entry, bookmark, subscription) from a branch | NetBox's routing: a journal entry lands in the branch; a bookmark or subscription lands in main. Neither changes a plugin row or contacts Kea |

With no branch active, a plugin-owned URL callback with an unsafe method is checked for an
unusable branch selection. The selection follows NBB's precedence (`NBB/utilities.py:559-597`):

| Request | NBB result | Plugin outcome |
|---|---|---|
| API request with `X-NetBox-Branch` naming an unknown or unready branch | 400 from NBB's middleware, before any plugin hook | NBB's 400 (upstream contract) |
| `_branch` query naming an unknown branch | 400 from NBB | NBB's 400 |
| `_branch` query naming an unready (merged, archived, failed) branch | No branch; cookie ignored | 409, code `branch_selection_unusable` |
| `_branch` query present and empty (API header absent) | Explicit switch to main; NBB removes the cookie from the request (`NBB/utilities.py:583-586`) | Served |
| `_branch` query absent, `active_branch` cookie naming an unknown or unready branch | No branch | 409, code `branch_selection_unusable` |
| No selector | Main | Served |
| UI request with an `X-NetBox-Branch` header | NBB ignores the header | The header is ignored too |

The predicate, in `process_view`: no active branch, and either the `_branch` query is present and
non-empty, or it is absent and the `active_branch` cookie is present. An API header never reaches
this point without an active branch, because NBB refuses it first. For UI and REST the refusal
renders as in the contract above. For HTMX, a Django error message is queued (the branch is not
usable, nothing changed, the page now shows main), and the 409, with an empty body, carries
`HX-Redirect` to the Server list, reversed, with `?_branch=` empty, the explicit switch to main.
Nothing in the target comes from the request. `HX-Refresh` is not used here: the current URL can
carry `?_branch=<id>` for a deleted branch, which NBB answers with 400 (`NBB/utilities.py:566-568`);
and the originating page can 404 on main, which renders the 404 template and consumes the message
(`DJ/contrib/messages/storage/base.py:67-72,128-140`) before NBB replaces the 404 with a redirect
to `/` (`NBB/middleware.py:69-86`). The Server list takes no object id (`K/netbox_kea/urls.py:60`),
so it cannot 404.

Earlier framework refusals are also outcomes: NBB's 400s above, and Django's CSRF 403, which runs
before `process_view`.

Residual cases, documented: NBB deletes a stale cookie on the response (`NBB/middleware.py:62-89`),
so a retry after the 409, or a POST from a page whose cookie another tab cleared, carries no
selector and runs on main. A request without a selector asks for main, so this is not a fallback:
the stale-selector refusal applies only to a request that names a branch. That is how every NetBox
view behaves after a merge. Such a write has the same Kea effect in any schema, and its follow-up
rows land in main.

Without netbox-branching every guard is a no-op and behaviour does not change, except the
`sync_vrf` `PROTECT`, which applies everywhere.

### Owner: `netbox_kea/branching.py`

- `active_branch()`: the active Branch, or `None` when netbox-branching is absent. The only
  import of `netbox_branching` in the plugin.
- `refuse_in_branch(operation)`: raises `BranchActive` when a branch is active. `BranchActive` is
  not a `KeaException`, so a view's `except KeaException` cannot turn it into a Kea error.
- `is_branchable(model)`: the resolver. For a plugin model, `True` iff it has a concrete
  `ForeignKey` or `OneToOneField` whose `on_delete` writes (`CASCADE`, `SET_NULL`, `SET_DEFAULT`,
  `SET(...)`) to a model outside the plugin that `supports_branching()` accepts; otherwise
  `False`. `None` for models of other apps. Today it returns `False` for all three models.
- `BranchRefusalMiddleware` (in `PluginConfig.middleware`): for a request whose resolved callback
  module is inside `netbox_kea`, refuses an unsafe method when a branch is active or when a branch
  selector is unusable (the predicate above), both in `process_view`; adds the sources header to
  responses from plugin-owned callbacks and the GraphQL endpoint while a branch is active; renders
  `BranchActive` from `process_exception` as the 409 (HTMX: message plus `HX-Refresh`).
- `register()`, from `ready()`: registers the resolver and the model receivers.
  `register_model_feature('branching', supports_branching)` evaluates lazily
  (`NBB/__init__.py:133`), and netbox-branching must be listed last, so the resolver is in place
  before any routing decision.

#### Implementation note (2026-09-29, increment 1)

Increment 1 changed how the resolver is built. The contract did not change. `is_branchable(model)`
returns `False` for every netbox_kea model, historical models included, and `None` for every other
model. It evaluates no rule at run time. The reasons:

- `supports_branching()` catches any exception from a resolver, logs it, and falls back to the
  `ChangeLoggingMixin` check (`NBB/utilities.py`, `supports_branching`). A resolver that raises
  therefore makes `Server` branchable. That fails open.
- The foreign-key rule must be transitive. `KeaDhcpLink.server` is `CASCADE` to `Server`, so if
  `Server` needed a branch copy, `KeaDhcpLink` would need one too.

Guard 2 is now the only place that computes the rule, transitively, from the live models. It fails,
and names the foreign-key path, when a plugin model would need a branch copy. It also asserts that
`supports_branching()` is `False` for every plugin model.

#### Implementation note (2026-09-30, increment 3)

The middleware follows the contract, with these facts from netbox-branching 1.2.1 at run time:

- An API request whose `X-NetBox-Branch` header names an unready branch does not get NBB's 400.
  `get_active_branch()` returns its `HttpResponseBadRequest` instead of raising, and the request
  processor activates that response as the branch, so the first branchable query fails with a 500.
  For a plugin-owned callback, the middleware returns that 400, whatever the method. A header that
  names an unknown branch still gets the 400 from NBB's middleware.
- GraphQL is not a plugin callback, so the same header on a GraphQL request still reaches NBB's own
  failure: `server_list` answers from main without the sources header, and a branchable field
  returns a GraphQL error.
- NBB reads an empty `active_branch` cookie as no branch, so the predicate does too: the cookie
  counts only when it is not empty.
- The plugin templates extend five different NetBox templates and share no base. The banner is a
  `navbar` template extension, the one hook on every page, and it renders only for a plugin-owned
  callback. The navbar is narrow and NetBox renders it twice, so the banner is a "Kea read-only"
  button beside netbox-branching's selector; its menu holds the full wording. The 409 page links
  to the Server list with `?_branch=`, the same target as the HTMX stale-selector refusal.
- `process_exception` renders `BranchActive` from any view, not only from plugin-owned callbacks.
- Guard 1 fills each URL pattern itself, because two plugin patterns can share a URL name (#246),
  and checks that the URL resolves back to the same view. It sends GET, HEAD and OPTIONS, and
  POST, PUT, PATCH and DELETE, to every plugin URL. Its Kea replies come from `kea_recordings/`; a
  read may send only the commands that this read-only Kea answers. It found that the format-suffix
  URLs of the four REST Kea actions (`servers/<pk>/leases4.json` and the like) answer 500 on main,
  because the action methods take no `format` argument (#245). The guard compares them with main
  and does not require 200.

### Sinks

- **Kea transport.** `KeaClient.command()` takes a `KeaCommand` enum member and a target,
  `Family | None` (`None` addresses the Control Agent, as `status-get` and `version-get` do today,
  `K/netbox_kea/views/server.py:152-158`), not a string. A test pins the wire payload of each form. Each member carries a `read` or `write` kind. The call sites that build names today
  (`kea.py:1430-1431,1727,1985,2010-2011`, `views/leases.py:506`) are refactored to members.
  `command()` refuses a `write` member when the client is branch-bound or a branch is active. `Server.get_client()` binds the client when a
  branch is active; `clone()` keeps the binding (thread-pool workers do not inherit context
  variables, and `clone()` copies fields explicitly, `K/netbox_kea/kea.py:772-789`). A string
  command is a type error (mypy) and a `TypeError` at runtime.
- **Plugin models.** `pre_save` and `pre_delete` receivers on `Server`, `SyncConfig` and
  `KeaDhcpLink` call `refuse_in_branch()`. A `pre_delete` receiver disables Django's fast delete,
  so queryset `delete()` reaches it. `JobsMixin.delete()` runs its job deletion and the collector
  in one transaction (`NB/netbox/models/features.py:513-520`), so the refusal rolls the job
  deletion back.

#### Implementation note (2026-09-30, increment 4)

The sinks follow the design, with these facts from the code:

- `kea.py` imports nothing from Django: the black-box suite imports it on the host through a
  symlink. So the transport does not import `branching.py`. Instead, every `KeaClient` must carry a
  write guard: `write_guard` is a required keyword argument, and `command()` asks it before each
  write member. `clone()` keeps it.
- The plugin's own clients come only from `Server.get_client()`, which passes `branching.bind()`, a
  `BranchBinding` that holds the branch that was active when the client was built. It raises
  `BranchActive` when that branch is set, or else when a branch is active at call time. Guard 3
  pins `get_client()` as the only build site in the runtime package, with `branching.bind()` as its
  guard.
- A caller outside the plugin that builds its own `KeaClient`, such as a Custom Script, passes its
  own guard and owns that choice. The contract row for a non-HTTP caller holds for a client from
  `Server.get_client()`, and for any client whose guard is a `BranchBinding`.
- `register()` connects the model receivers only when netbox-branching is installed, because a
  `pre_delete` receiver turns off Django's fast delete. They cover every netbox_kea model from the
  app registry, the set that the resolver keeps in main.
- The job guard sets `job.error` and raises `JobFailed`. NetBox saves the error when it marks the
  job failed.
- The OpenGrep rules that matched command strings now match `KeaCommand` members.
- The browser-suite harness client takes a wire name and turns it into a member: the unit suite
  loads `test_workflows.py` standalone, where `KeaCommand` cannot be imported. The state check of
  the browser branching test reads the configuration hash from `config-get` and the reservations
  through `reservation-get-page`, because the plugin sends neither `config-hash-get` nor
  `reservation-get-all`.

### Model decisions

| Model | Branchable | Reason |
|---|---|---|
| `Server` | No (resolver) | One row per pk: the Kea client and the Redis keys use main's current connection fields; sync never copies censored passwords into a branch |
| `SyncConfig` | No (plain model) | Global singleton |
| `KeaDhcpLink` | No (plain model, resolver) | Its only foreign keys go to `Server` (main-only) and `ContentType` (exempt) |
| ADR 0006 ownership link | No (resolver) | `CASCADE` keys to IPAddress, Prefix, IPRange. A delete of a linked object in a branch reaches main's link table, and the `pre_delete` receiver refuses it. Merge and revert delete on main, where `CASCADE` removes the links. Design: `docs/design/ipam-ownership-branching.md` |

`Server.sync_vrf` becomes `PROTECT`. A VRF delete, in a branch or in main, is refused while a
Server syncs into that VRF. Django's collector raises `ProtectedError` during collection, before
deletion signals or writes (`DJ/db/models/deletion.py:343-357`); NetBox's delete views render it.

### Hidden writes on GET

- A data migration (`fake_on_branch = True`) creates the `SyncConfig` singleton and applies the
  one-time backfill. `SyncConfig.get()` becomes a plain read, and `backfill_applied` is removed.
- Redis writes on display stay: the cache is derived from live Kea and, with Server main-only, is
  the same for main and every branch.

### Merge, discard, revert

No plugin row exists in a branch, and no Server change reaches a branch changelog, so no plugin
validator is registered. Known limits, documented:

- A Tag delete in a branch removes the Tag's `TaggedItem` rows for main's Servers in the branch
  (TaggedItem is branchable), and NetBox writes the Server change record to main's changelog
  before any merge (see D9). Merge replays the Tag delete in main. A revert of that merge
  does not restore the Servers' tag assignments.
- A link whose netbox_dhcp target a merged branch deleted stays until the next import relinks
  (today's behaviour for a delete in main).

### Migrations

The `sync_vrf` migration alters a model that is no longer branchable, so branch migrate fakes it
(`NBB/models/branches.py:138-186`). The `SyncConfig` data migration sets `fake_on_branch = True`.
The release ships a migration, so `migrate` refreshes stored `ObjectType.features` for Server
(`NB/core/signals.py:52-75`).

### Mechanical guards

1. URL-tree test in a provisioned branch, over every URL whose callback module is inside
   `netbox_kea`, with a superuser and fixtures that make every route resolve to a real object.
   Unsafe methods: the 409 page or REST code, zero Kea commands, and no INSERT, UPDATE or DELETE
   on either connection. Safe methods: the status the same request gets on main (200 for pages),
   no INSERT, UPDATE or DELETE on either connection, and no refused Kea command. A route whose
   arguments the test cannot build fails by name.
2. Branchability pin: compute, from `_meta` of every plugin model (auto-created ones included),
   the relations that a delete in a branch reaches, and assert that they equal a pinned set of
   `(model.field, on_delete)`. Each one must be a concrete `CASCADE` key on a model with the refusal
   receiver. Assert that `supports_branching()` is `False` for every plugin model. The message says
   that a change needs a design decision (`docs/design/ipam-ownership-branching.md`).
3. Kea transport: an AST scan asserts that the runtime package has exactly one HTTP send, inside
   `KeaClient.command()`. A test pins the `read` and `write` members of `KeaCommand` as two
   explicit sets written in the test, independent of the enum, and fails when a member is added,
   removed or reclassified. The refusal test is parameterized over every `write` member, on a
   branch-bound client and on a clone in a thread.
4. Migration check: a migration with `RunPython`, `RunSQL` or `SeparateDatabaseAndState` sets
   `fake_on_branch` explicitly, and a migration that writes only main-only plugin tables sets it
   to `True`.

### CI

A new job: NetBox 4.7.0, netbox-branching 1.2.1, netbox-plugin-dhcp 0.2.0, `DynamicSchemaDict`,
`BranchAwareRouter`, netbox_branching last. It runs `test_branching.py` with an environment
variable that turns the module's `importorskip` into a failure, so the job cannot pass with the
tests skipped. With real provisioning, it covers guards 1 to 4 and:

- a provisioned branch holds no `netbox_kea_*` table; stored `ObjectType.features` for Server has
  no `branching`; a Server edited in main after the branch was created reads with main's values in
  the branch (fails with the resolver removed);
- a VRF delete in a branch, with a Server syncing into it, raises `ProtectedError`; main unchanged;
- instance writes of the three plugin models in a branch raise `BranchActive`; main unchanged;
- a mutating Kea command in a branch, also from a cloned client in a thread, raises
  `BranchActive`, and the Kea stub records nothing;
- the selector table above, parameterized (UI and API, header, query empty, unknown, unready and
  ready, cookie ready, stale and unknown), with CSRF enforced: each row's outcome, with both
  databases unchanged and zero Kea commands on a refusal;
- in a branch, a plugin page, a REST read and a GraphQL `server_list` query carry the sources
  header; the IPAddress panel shows its label; a `core.Job` API read carries no sources header;
- a browser test (Playwright, `tests/ui/test_branching_refusal.py`) clicks the reservation "Sync
  all" button in these states: branch active; branch merged elsewhere (stale cookie); page opened
  through the branch selector (`?_branch=<id>` in the URL) and the branch then deleted; stale cookie
  with the page's Server deleted in main, with `DEBUG=False`. The recovery cases
  (`test_stale_selector_redirect_shows_refusal`) also run as a user with `view_server`, a user
  without it (403 page) and an anonymous user (login page). It runs in its own CI job on the
  netbox-branching variant of the compose harness (`tests/docker/docker-compose.branching.yml`:
  NetBox 4.7.0, netbox-branching 1.2.1, `DEBUG=False`). The harness is the real deployment shape,
  and netbox-branching supports NetBox 4.7 only, so the variant is not in the NetBox matrix. Kea is
  live there, so each case proves by state that nothing changed: the configuration hash,
  reservations and leases of both daemons, and the NetBox IP addresses, are the same before and
  after. The claim of zero Kea commands during the refused POST, reads included, belongs to guard 1
  and the selector table, which count commands with `kea_stub`. Each case also asserts the visible
  toast and, in the stale cases, a final page on main (the Server list, the 403 page or the login
  page, by authorization);
- a Tag on a Server deleted in a branch: the Server change record is in main's changelog and no
  Server ChangeDiff exists; after merge, main's Server has lost the tag, its other fields
  (credentials included) are unchanged, and the Kea stub recorded nothing;
- a netbox_dhcp Subnet delete in a branch leaves main's link; merge leaves it dangling; the next
  import relinks.

### Out of scope, follow-ups

- `Server.clean()` sends `version-get` (network I/O in model validation). Under this design no
  merge replays a Server change, so branching does not need the move; it is adjacent debt.
- `snapshot()` before plugin updates (13 sites) and `KeaIpamSyncJob` writing IPAM with no
  ObjectChange: main changelog defects. With branching, merge conflict detection cannot see the
  job's edits.
- netbox_dhcp target deletion receivers for `KeaDhcpLink` (D3).
- Disabling mutation controls in a branch (D12).
- The reservation edit and delete GET pages invalidate the Redis cache through `MutationScope`
  (`views/reservation_mutations.py:218-249`); harmless under this design, but a GET should not.

### Increments

1. Resolver, `sync_vrf` `PROTECT`, the CI job, guards 2 and 4. Done when a provisioned branch holds
   no `netbox_kea_*` table and a Server edited in main after branch creation reads with main's
   values in the branch; both tests fail with the resolver removed. The VRF `PROTECT` test passes.
2. `SyncConfig` seeding.
3. Middleware (branch refusal, stale-selector predicate, sources header), banner, IPAddress panel,
   guard 1.
4. `KeaCommand` transport refactor with the client binding, guard 3; model receivers; job guard.
