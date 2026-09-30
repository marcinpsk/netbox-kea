# netbox-branching compatibility

## 0. Review record

Status: **RATIFY r6** (core and stale-selector), round 6, 2026-09-29, for NetBox 4.7.0 with
netbox-branching 1.2.1 and branches created after the release that adds support. The ratification
is by source inspection; the CI job in the design verifies it at runtime. Round 5 was the cap; the
operator authorized one confirmation round on r6, limited to the round-5 findings and new
blockers. Rounds 1 to 5: NOT RATIFIED (four, three, two, one and one blockers). Every blocker
since round 3 was in the refusal UX of the stale-selector mechanism; none let a write escape.

Blind co-design: Claude (Opus 5.5) drafted from this brief. `gpt-6-astra` at reasoning effort high
drafted in a fresh read-only context. Its packet held the problem, the operator decision, the
constraints, the acceptance conditions, the evidence survey, and the netbox-branching mechanics
that the sibling reviews reported, marked "verify before relying". It did not get the sibling
designs, the candidate shapes below, or the Claude draft; it was told not to read this file.

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
- Deliverable: this record, ratified. No implementation in this change.

### Constraints

- netbox-branching stays optional. Without it, behaviour does not change.
- No backwards-compatibility layers; remove obsolete paths.
- Fail fast and visibly; no silent fallback to main.
- The plugin was never compatible with netbox-branching, so no deployment has a branch that
  predates the release that adds support (the netbox-data-import operator decision, applied here).

### Acceptance conditions

1. With a branch active, every plugin entry point (UI view, REST API, GraphQL, background job,
   template extension on a core page) has one documented outcome. A read returns data and says
   which schema it read. A write is refused before any Kea command and before any database write.
   No outcome is an unhandled 500.
2. A NetBox core action executed in a branch does not change the plugin's rows in main, and sends
   no Kea command. A change to a global object (an exempt or non-branchable core model, such as
   CustomField, User, `core.Job`, Bookmark) lands in main, and so do its effects on plugin rows.
   (Wording added in r2, after round 1 finding B1.)
3. After a branch merges or is discarded, main's plugin rows are consistent with main's NetBox
   objects.
4. A CI job with netbox-branching on NetBox 4.7 runs tests that fail when 1 to 3 regress.
5. A mechanical guard stops a new write path from bypassing the refusal.

### Prior art (sibling plugins, same NetBox and netbox-branching versions)

- netbox-data-import: main only, ratified in round 4. A resolver makes a plugin model branchable
  only when it has a database foreign key to a branchable model outside the plugin. Middleware
  refuses every plugin URL callback with 409. Jobs and the profile lock refuse. A revert validator
  refuses reverts that cannot restore plugin data. `fake_on_branch = True` on data migrations.
  Guards: a URL-tree walk, a pinned branchable set, a migration check.
- netbox-librenms: full support, r4, not ratified. Nested transactions on `default` and the branch
  alias (netbox-branching writes `ChangeDiff` on `default`), `lock_timeout` for cycles across the
  two sessions of one request, `snapshot()` before every update, cache keys per alias. Most of its
  twelve blockers came from the two-connection transaction model.

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
- `Server.get_client()` builds the Kea client from the Server row (`models.py:238-291`), and
  `Server.clean()` sends `version-get` on every create and edit (`models.py:293-336`).
- Only branchable tables are copied into a branch (`NBB/utilities.py:293-317`). Branch tables share
  main's id sequences (`NBB/provisioning.py:346-356`).

Entry points (95 routes, 88 view classes; 44 gated by `_KeaChangeMixin`, `views/_base.py:63-84`):

- Kea writes: lease add, edit, bulk delete and import; subnet wipe; reservation add, edit, delete
  and import; every configuration change through `config_write`; DHCP enable and disable. All are
  POST. The Kea change is live before any follow-up database write (JournalEntry, IPAM sync).
- Database writes: Server CRUD (generic views, REST viewset), `sync-toggle`, `sync-jobs` POST,
  `sync-now` (enqueues `core.Job`), lease and reservation sync to IPAM, the DHCP-plugin import
  (netbox_dhcp objects, IPAM rows, `KeaDhcpLink`).
- No GET handler reaches a Kea write. Two GET handlers write the database: `sync-jobs/` and
  `servers/<pk>/sync_status` call `SyncConfig.get()` (`get_or_create` and a one-time backfill,
  `models.py:449-480`). Display reads write Redis. One POST only reads: lease bulk delete without
  `_confirm` renders the confirmation page (`views/leases.py:532-546`).
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

## Divergence table (blind merge)

Both drafts chose: a `netbox_kea/branching.py` owner; refusal in plugin middleware (branch
activation runs earlier, in `CoreMiddleware` request processors, and a processor cannot refuse,
`NB/utilities/request.py:131-142`); a deny-by-default read-command set in `KeaClient.command()`
(`kea.py:723-765`, the single HTTP funnel); `KeaIpamSyncJob` stays main-only; `SyncConfig`
main-only; the ADR 0006 link model branchable; a CI job with a real provisioned branch.
Convergence narrows where to look; it closes nothing.

| # | Decision | Claude | Codex | Evidence | Disposition |
|---|---|---|---|---|---|
| D1 | Is `Server` branchable | No: resolver `False`. One row per pk, so the Kea client and the Redis keys always use main's current connection fields | Yes. Branch reads use the branch copy's connection fields. Branch sync is refused when main changed Server create, delete or connection fields; the operator recreates the branch | Server changelogs censor passwords (`models.py:338-358`), so a synced connection edit writes a censored password into the branch copy. Sync selects by branchable type (`NBB/models/branches.py:412-426`), so a main-only Server never syncs. Under codex's shape, any main edit of a Server's connection fields blocks sync of every open branch | Claude's. Codex's shape needs a cache bypass (D6), sync and merge validators (D8), and a stale-copy policy; a main-only Server needs none of them. Round 1 is to attack it |
| D2 | `Server.sync_vrf` | `SET_NULL` to `PROTECT` | Keep `SET_NULL`; make the reverse relation visible so NetBox snapshots the Server before it nulls the key | With Server main-only, a `SET_NULL` cascade from a branch VRF delete runs on the branch connection and resolves `netbox_kea_server` to main, so main changes at once. `PROTECT` raises in `Collector.collect()` before any write or signal. Today a VRF delete silently moves the next sync into the global VRF | Claude's, forced by D1. **Operator-visible change in main:** a VRF that a Server syncs into can no longer be deleted until the Server stops using it |
| D3 | `KeaDhcpLink` | Main-only; a link to a deleted target stays until the next import relinks (today's behaviour) | Branchable, plus receivers that delete links when a netbox_dhcp target is deleted | A receiver running in a branch against a main-only table deletes main rows. Branchable links need revert and sync validators (sync writes synthetic DELETEs for them, `branches.py:837-897`). Readers already treat a missing target as absent (`integrations/dhcp_plugin.py:650-654`) | Claude's. The receivers improve main without branching too; they are a follow-up, not a branching requirement. Reopen if a reader treats a dangling link as a live object |
| D4 | Refusal seams | Middleware (by HTTP method), the Kea transport, `pre_save`/`pre_delete` receivers on the three plugin models, a job guard | Middleware (by classified operation), view dispatch again for `AsyncViewJob`, REST action guards, guards on every domain write, the Kea transport, an AST write-boundary gate | Every plugin HTTP entry, including those that enqueue `AsyncViewJob` or `AsyncAPIJob`, passes plugin middleware first. A job can only be queued by a request that the middleware already let through, so a branch-context plugin job cannot exist. Non-HTTP callers (Custom Scripts) get branch routing for core rows, and the Kea and model sinks refuse the rest | Claude's. Codex's extra layers guard paths that no caller reaches. Reopen on a caller that reaches plugin write code with a branch active and without plugin middleware |
| D5 | Read versus write | HTTP method; no exceptions | A classified inventory of every operation | The one read-only POST (lease bulk delete confirmation, `views/leases.py:532-546`) leads only to a delete that is refused anyway. No GET reaches a Kea write | Claude's |
| D6 | Redis caches in a branch | Shared; valid because D1 gives one connection per Server | Bypass both caches in a branch | Follows D1 | Claude's |
| D7 | `SyncConfig.get()` on GET | A data migration creates the singleton and applies the backfill; `get()` becomes a read | A separate read-only lookup for branches; creation stays in guarded main operations | Codex's keeps two paths to one row | Claude's |
| D8 | Sync, merge and revert validators | None: no plugin row exists in a branch | Refuse merge, sync and revert for Server create, delete, connection changes, link changes, and deletes of link-target types | Follows D1 and D3 | Claude's |
| D9 | `Server.clean()` sends `version-get` | Not addressed | Move the connectivity check out of `clean()` into the form and serializer validation | Merge replays every ObjectChange in the branch schema, not only branchable types (`get_unmerged_changes`, `NBB/models/branches.py:429-435`). A Tag delete in a branch records an UPDATE for each tagged Server (`NB/core/signals.py:218-250`). Replay runs `full_clean()` (`NBB/utilities.py:512-513`), so a merge contacts Kea, and fails when Kea is down | Codex's, accepted. Applies under D1 as well |
| D10 | Kea client used from a thread pool | Context variable only | The client records the branch state at construction; `clone()` keeps it | Thread-pool workers do not inherit context variables; clones run in pools (`kea.py:768-785`, `views/leases.py:942`) | Codex's, accepted |
| D11 | Stale branch selection | Not handled; the request runs on main, as for every NetBox view | Refuse with 400 `invalid_branch_selection` | A stale cookie or unready query runs on main (`NBB/utilities.py:548-597`, `NBB/middleware.py:53-89`). The Kea effect of a write is the same in any schema, and the follow-up rows land in main, which is where a merge would put them | Claude's. Round 1 may attack it |
| D12 | Write controls in the UI | A banner; the IPAddress panel hides its add links | Disable mutation controls | 44 mutation views. Refusal already meets condition 1 | Claude's; disabling controls is a follow-up |
| D13 | Early Server delete guard | `pre_delete` receiver | At `delete()`, because `JobsMixin.delete()` removes jobs before the collector runs | `JobsMixin.delete()` wraps the job deletion and `super().delete()` in one `atomic(using=...)` (`NB/netbox/models/features.py:513-520`), so a `pre_delete` refusal rolls the job deletion back | REFUTED, codex's claim. Reopen if NetBox removes that transaction |
| D14 | Startup validation of `exempt_models` | None | Reject exemptions that break the required branchable set | No plugin model is branchable today, so no exemption can break the set | Deferred to the ADR 0006 link model, which is the first branchable plugin model |
| D15 | Branch table footprint check per request | None | Validate before reads and core actions | Constraint: no branch predates this release. Guard 2 blocks a new branchable model without a design decision | Rejected |
| D16 | REST refusal code | `branch_not_supported` | `branch_write_refused` | Reads are supported | Codex's |
| D17 | CI variants | netbox-plugin-dhcp present | Present and absent | Nothing in the design differs with the DHCP plugin absent | Claude's |


### Round 1 dispositions (r1)

Reviewer: `gpt-6-astra`, effort high, read-only, fresh context. Verdict: NOT RATIFIED r1, four
blockers, three non-blocking. It agreed with D1, D2, D3, D5 to D8, D10, D12 to D17.

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| B1 | A CustomField delete or rename in a branch updates `custom_field_data` of main's Servers by queryset `update()`, which no model receiver sees | `NB/extras/signals.py:64-80`; `NB/utilities/querysets.py:54-74`; `extras.customfield` is in `EXEMPT_MODELS` (`NBB/constants.py`) | REFUTED as a defect. CustomField is exempt, so the delete itself lands in main, and its effect on Servers lands with it; main stays consistent with main's CustomFields. Discard restores neither, by design. Acceptance condition 2 now states the global-object rule. Reopen if branching makes CustomField branchable |
| B2 | A Custom Script in a branch reaches plugin write code without plugin middleware: `sync_lease_to_netbox()` writes an IPAddress; `config_write` sends a Kea read before the transport refuses the mutation; queryset `update()` and `bulk_create()` on plugin models skip model signals | `NB/extras/jobs.py:398-403`; `K/netbox_kea/sync.py:507-592`; `K/netbox_kea/config_write.py:111-120` | Mechanism accepted; D4's reopening condition is met. Not accepted as a blocker for the entry-point contract: acceptance condition 1 is about plugin entry points, and a script is administrator code. The IPAddress lands in the branch the script runs in, which is the schema its author chose. A read before the refused mutation changes nothing. r2 adds an explicit contract row for non-HTTP callers: Kea mutations refused (transport), instance writes of plugin models refused (receivers), core rows follow branch routing, and bulk queryset methods and raw SQL on plugin models are outside the contract, as for every non-branchable model in NetBox |
| B3 | A stale selector (cookie for a merged or archived branch, or an unready `?_branch=`) runs the request on main, so a plugin write from a branch page lands in main | `NBB/utilities.py:565-597` returns `None` without refusing; `NBB/middleware.py:53-62` | Accepted. D11 reversed: the constraint forbids a silent fallback to main |
| B4 | Acceptance condition 1 says a read "says which schema it read", but only plugin pages get a banner; REST, GraphQL and the IPAddress panel say nothing | `K/netbox_kea/graphql.py:54-59`; `K/netbox_kea/template_extensions.py:38-73` | Accepted. REST responses from plugin callbacks carry a sources header; the panel shows a label; GraphQL, served by a core endpoint, is documented (Server is always main) |
| N1 | D9's premise is wrong: a Server change record created by a Tag delete in a branch is written to main's changelog (the ObjectChange takes the loaded Server's database through its ContentType foreign key), and NBB records no ChangeDiff for it; merge replays only the branch schema's changelog | Traced: `NB/netbox/models/features.py:117-127` -> Django GenericForeignKey and FK descriptor set the new row's database -> `NBB/database.py:49-50` declines -> Django falls back to the instance database; `NBB/signal_receivers.py:125-127` | Accepted. Under D1 no Server change reaches a branch changelog, so no merge replays `Server.clean()`. D9 leaves this design and becomes a follow-up (model validation that does network I/O). The Tag-delete behaviour is documented and pinned by a test |
| N2 | Guards are too weak: guard 1 accepts 403 and 404; the increment-1 claim that each test fails without the resolver is false (PROTECT refuses the VRF delete either way); guard 3 misses computed command names such as `network{version}-get` (`K/netbox_kea/kea.py:1426-1427`); guard 4 accepts `fake_on_branch = False`; `importorskip` lets the branching job pass with nothing run | Read | Accepted, all five |
| N3 | Core metadata on a main-only Server: a journal entry from a branch lands in the branch (JournalEntry is branchable); a bookmark lands in main (plain model). Neither passes plugin middleware | `NB/extras/models/models.py:964,1027`; `NB/netbox/models/features.py:790-792` | Accepted: documented. Neither changes a plugin row or contacts Kea |

### Changes r1 to r2

1. Acceptance condition 2 states the global-object rule (B1).
2. The contract has a row for non-HTTP callers, and says what is outside it (B2).
3. Plugin middleware refuses an unsafe plugin request that carries a branch selector while no
   branch is active, except the explicit switch to main (B3).
4. REST sources header, IPAddress panel label, GraphQL documented (B4).
5. The `Server.clean()` validation move (D9) is removed from this design and listed as a
   follow-up. The Tag-delete path is documented and tested (N1).
6. Guards 1, 3 and 4 and the CI job are tightened; the increment-1 done-condition names only
   resolver-sensitive tests (N2).
7. Journal and bookmark outcomes are documented (N3).

### Round 2 dispositions (r2)

Verdict: NOT RATIFIED r2. B1 and B2 WITHDRAWN by the reviewer; N1 and N3 CLOSED; B3, B4 and N2
NOT-CLOSED, restated as the findings below.

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R2-B1 | The stale-selector predicate ignores NBB's precedence: NBB reads `X-NetBox-Branch` only for API requests, so a UI request with that header and `?_branch=` is an explicit switch to main that r2 refuses; an unknown query or API header gets NBB's 400 before any plugin hook, not the promised 409 | `NBB/utilities.py:559-597`; `NBB/middleware.py:47-59` | Accepted. r3 defines the predicate with NBB's precedence and picks `process_view`; NBB's 400s stay part of the contract |
| R2-B2 | After the plugin's 409, NBB deletes the stale cookie on the response (`branch_change` when a cookie is present and no branch is active), so a retry of the same POST runs on main. A form rendered in a branch whose cookie another tab cleared also runs on main | `NBB/middleware.py:62-89` | Mechanism accepted. Disputed as a blocker: the first request is refused visibly, so the fallback is not silent, and a later POST without a selector is the same as every NetBox view after a merge. The Kea effect of such a write is the same in any schema, and its follow-up rows land in main, where a merge would have put them. Second finding on the stale-selector mechanism (after B3); it is now a split candidate, and round 3 is asked for two verdicts |
| R2-B3 | GraphQL has no response-level source indication | `K/netbox_kea/graphql.py:54-59` | Accepted. r3: while a branch is active, the plugin middleware adds the sources header to every response, whatever the endpoint |
| R2-N1 | Guard 3's "any other expression fails" rejects today's code: a local name bound to an f-string (`K/netbox_kea/kea.py:1426-1427`) and a forwarded parameter (`kea.py:800-808`) | Read | Accepted: bounded resolution, below |
| R2-N2 | Guard 3 cannot detect a write command added to `READ_COMMANDS` | Read | Accepted: the read and write sets are disjoint and pinned, and the refusal test runs over every write command |

### Changes r2 to r3

1. Stale selector: a precedence-correct predicate, evaluated in `process_view`; NBB's own 400s are
   documented as part of the contract; the residual cases of R2-B2 are documented (R2-B1, R2-B2).
2. The sources header is on every response while a branch is active, including GraphQL and core
   pages (R2-B3).
3. Guard 3: bounded resolution of command expressions; disjoint pinned read and write sets; the
   refusal test covers every write command (R2-N1, R2-N2).

### Round 3 dispositions (r3)

Two verdicts, as for a split candidate. Verdict A: NOT RATIFIED r3 core. Verdict B: NOT RATIFIED
r3 stale-selector. The reviewer found that the core cannot drop the stale-selector mechanism
without a change to the "no silent fallback" constraint, so a split needs an operator decision;
the mechanism stays in scope. R2-B1, R2-B3 and R2-N2 CLOSED. R2-B2 NOT-CLOSED only through R3-B1:
the reviewer withdrew the claim that a selector-free retry after a *visible* refusal is a
blocker. R2-N1 NOT-CLOSED (R3-N1).

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R3-B1 [A, B] | HTMX does not swap a 4xx response by default, and `HX-Retarget`/`HX-Reswap` do not enable a swap, so a 409 to an HTMX POST (the reservation "Sync all" button) is invisible. NBB then clears the stale cookie, and the next click writes IPAM in main | `NB/static/django_htmx/htmx-2.js:264-267,4848-4896`; `K/netbox_kea/templates/netbox_kea/inc/reservation_sync_badge.html:18-21`; `NBB/middleware.py:62-89` | Accepted. htmx handles `HX-Refresh` and `HX-Redirect` before the status rules (`htmx-2.js:4831-4843`). r4: an HTMX refusal queues a Django error message and returns the 409 with `HX-Refresh: true`. The page reloads and shows the message (NetBox renders queued messages as toasts). In the stale case, the reload runs on main because NBB cleared the cookie, and the message says the branch is not usable and nothing changed |
| R3-B2 [A] | The global header claims `netbox=branch:<id>`, which is false for exempt models (`core.Job`) and for streamed exports evaluated after the branch context ends | `NBB/constants.py:57-68`; `NB/netbox/views/generic/bulk_views.py:142-148`; `NB/netbox/middleware.py:63-64` | Accepted. r4 narrows the header to what the plugin controls: `kea=live; plugin=main; branch=<schema_id>` (the active routing context, not the source of other objects), only on responses from plugin-owned callbacks and the GraphQL endpoint. netbox_kea rows are main-only, so `plugin=main` holds for a streamed body as well |
| R3-N1 | Guard 3's expression resolution still rejects today's code: `action`, `command_suffix`, `self.dhcp_version`, a tuple-bound command | `K/netbox_kea/kea.py:1723,1981,2006-2007`; `K/netbox_kea/views/leases.py:504` | Accepted, with a different fix: a type constraint instead of an AST resolver. `KeaClient.command()` takes a `KeaCommand` enum member and a family, not a string; each member has a pinned `read` or `write` kind. The call sites are refactored to members. Guard 3 checks the pinned classification and that no other HTTP send exists |
| R3-N2 | CSRF middleware can return 403 before `process_view` | `DJ/core/handlers/base.py:183-189` | Accepted: documented as an earlier framework refusal; the CI selector test runs with CSRF enforced |
| R3-N3 | "Every response" exceeds what the middleware controls (an outer `CommonMiddleware` slash redirect replaces the response) | `DJ/middleware/common.py:105-108` | Accepted: closed by R3-B2's narrowing to plugin callbacks and GraphQL |

### Changes r3 to r4

1. HTMX refusals: Django error message plus `HX-Refresh: true`, for the active-branch and the
   stale-selector refusal (R3-B1).
2. The sources header claims only `kea=live; plugin=main; branch=<schema_id>`, on plugin callbacks
   and GraphQL; the banner wording follows (R3-B2, R3-N3).
3. The Kea transport takes a `KeaCommand` enum member; guard 3 becomes a classification pin plus
   the single-send check (R3-N1).
4. Earlier framework refusals (CSRF 403, NBB 400) are documented (R3-N2).

### Round 4 dispositions (r4)

Verdict A: NOT RATIFIED r4 core, only through its dependency on B (no core blocker of its own).
Verdict B: NOT RATIFIED r4 stale-selector, one blocker. R3-B1, R3-B2, R3-N1, R3-N2, R3-N3, R2-B2
and R2-N1 CLOSED.

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R4-B1 [B] | A page opened through the branch selector keeps `?_branch=<id>` in its URL (`NBB/templates/netbox_branching/inc/branch_selector.html:13`). If that branch is deleted, a stale HTMX refusal reloads that URL, and NBB answers 400 "Invalid branch identifier" (`NBB/utilities.py:566-568`; `NBB/middleware.py:54-59`), so the queued message never renders and the user does not reach main. No write escapes | `NB/static/django_htmx/htmx-2.js:4833-4843` | Accepted. r5: a stale-selector HTMX refusal answers with `HX-Redirect` to the current page's path (from `HX-Current-URL`, same host only, else the Server list) with `?_branch=` empty, the explicit switch to main. The active-branch refusal keeps `HX-Refresh`. The HTMX 409 body is empty, so the queued message is not consumed before the next page (`DJ/contrib/messages/storage/base.py:67-72,128-140`) |
| R4-N1 [A] | `status-get` and `version-get` go to the Control Agent with no `service` (`K/netbox_kea/views/server.py:150-156,233-240`; `K/netbox_kea/kea.py:733-736`); "a member and a family" cannot express that | Read | Accepted: the target is `Family \| None`; `None` addresses the Control Agent. A test pins the wire payload |
| R4-N2 [A, B] | The browser test's "Kea stub unchanged" is wrong: the reload may send Kea reads | Read | Accepted: zero commands during the refused POST; zero mutating commands and unchanged rows over the whole interaction; assert the visible toast |

### Changes r4 to r5

1. Stale-selector HTMX refusal: `HX-Redirect` to the same path with `?_branch=`, empty 409 body;
   active-branch HTMX refusal: `HX-Refresh`, empty 409 body (R4-B1).
2. `KeaClient.command()` target is `Family | None` (R4-N1).
3. Browser test assertions split between the refused request and the whole interaction; the
   deleted-branch case added (R4-N2, R4-B1).

### Round 5 dispositions (r5)

Verdict A: NOT RATIFIED r5 core. No core blocker of its own; the core depends on the
stale-selector mechanism under the "no silent fallback" constraint, so the mechanism cannot be
split off without an operator change to that constraint. Verdict B: NOT RATIFIED r5
stale-selector, one blocker. R4-B1, R4-N1 and R4-N2 CLOSED.

| # | Finding | Verified by | Disposition |
|---|---|---|---|
| R5-B1 [B] | Redirecting a stale HTMX refusal to the originating path can reach a 404 (the Server was deleted in main). With `DEBUG=False` the 404 template renders and consumes the queued message, and NBB then replaces the 404 with a redirect to `/` (`NBB/middleware.py:69-86`), so the refusal message is lost. No write escapes | `DJ/contrib/messages/storage/base.py:67-72,128-140`; `NB/templates/base/base.html:77-78` | Accepted, with the reviewer's smallest fix: the target is always the Server list, reversed, with `?_branch=` |
| R5-N1 [B] | A path taken from `HX-Current-URL` can start with `//` and redirect off-site | `DJ/utils/http.py:274-315` | Closed by R5-B1's fix: no part of the target comes from the request |
| R5-N2 [B] | Dropping the query string loses page state and breaks pages that need it | `K/netbox_kea/views/reservation_mutations.py:201-213` | Closed by R5-B1's fix: a fixed destination promises no page restoration |

### Changes r5 to r6 (after the cap; no reviewer verdict)

1. The stale-selector HTMX refusal redirects to the Server list with `?_branch=`; nothing in the
   target comes from the request (R5-B1, R5-N1, R5-N2).
2. The browser test adds a case where the Server was deleted in main, with `DEBUG=False`.

r6 applies the reviewer's own stated smallest fix to the one remaining blocker. It has not been
reviewed. Round 5 was the cap, so ratifying r6 is the operator's decision (see Next action).

### Round 6 dispositions (r6, confirmation round)

Verdict A: RATIFY r6 core. Verdict B: RATIFY r6 stale-selector. No new blocker; no non-blocking
finding.

| # | Disposition | Evidence the reviewer traced |
|---|---|---|
| R5-B1 | CLOSED | The Server list takes no object id (`K/netbox_kea/urls.py:57`), so a deleted Server cannot make it 404. `?_branch=` empty reaches main with a stale or unknown cookie and no NBB lookup (`NBB/utilities.py:565-597`). A user without `view_server` gets a 403 page and an anonymous user the login page; both extend `base/base.html` and show the message, and NBB replaces only 404s (`NBB/middleware.py:71-86`) |
| R5-N1 | CLOSED | The target is reversed locally; nothing in it comes from the request |
| R5-N2 | CLOSED | The fixed destination needs no query parameters and promises no page restoration |

## Design r6

Design r2 with the changes r2 to r3 through r5 to r6 applied.

### Contract

Kea is live and shared by every branch. The plugin's own rows describe that live system, so they
exist in main only. In a branch the plugin is a viewer.

| Entry point, branch active | Outcome |
|---|---|
| Plugin-owned URL callback, GET, HEAD or OPTIONS (UI, HTMX) | Served. A banner on plugin pages says: Kea data is live; Kea servers, sync settings and DHCP plugin links come from main; other NetBox objects follow netbox-branching's routing for branch `<name>`; changes are refused |
| Response from a plugin-owned callback or the GraphQL endpoint while a branch is active | Carries `X-NetBox-Kea-Sources: kea=live; plugin=main; branch=<schema_id>`: Kea data in the response is live, and netbox_kea rows (Server, sync settings, links) come from main. `branch` names the active routing context; it does not state where other NetBox objects came from (netbox-branching's routing decides that, and exempt models such as `core.Job` read main) |
| Plugin-owned URL callback, any other method (UI) | HTTP 409 page naming the branch, with a link to switch to main (`?_branch=`) |
| Plugin-owned URL callback, any other method (HTMX) | A Django error message is queued, and the 409, with an empty body, carries `HX-Refresh: true`. htmx handles `HX-Refresh` before its status rules (`NB/static/django_htmx/htmx-2.js:4831-4843`), so the page reloads in the branch and shows the message. The empty body leaves the message unconsumed for the reload |
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
carry `?_branch=<id>` for a deleted branch, which NBB answers with 400 (R4-B1); and the originating
page can 404 on main, which consumes the message (R5-B1).

Earlier framework refusals are also outcomes: NBB's 400s above, and Django's CSRF 403, which runs
before `process_view`.

Residual cases, documented: NBB deletes a stale cookie on the response (`NBB/middleware.py:62-89`),
so a retry after the 409, or a POST from a page whose cookie another tab cleared, carries no
selector and runs on main. That is how every NetBox view behaves after a merge. Such a write has
the same Kea effect in any schema, and its follow-up rows land in main.

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
  `K/netbox_kea/views/server.py:150-156`), not a string. A test pins the wire payload of each form. Each member carries a `read` or `write` kind. The call sites that build names today
  (`kea.py:1426-1427,1723,1981,2006-2007`, `views/leases.py:504`) are refactored to members.
  `command()` refuses a `write` member when the client is branch-bound or a branch is active. `Server.get_client()` binds the client when a
  branch is active; `clone()` keeps the binding (thread-pool workers do not inherit context
  variables, and `clone()` copies fields explicitly, `K/netbox_kea/kea.py:768-785`). A string
  command is a type error (mypy) and a `TypeError` at runtime.
- **Plugin models.** `pre_save` and `pre_delete` receivers on `Server`, `SyncConfig` and
  `KeaDhcpLink` call `refuse_in_branch()`. A `pre_delete` receiver disables Django's fast delete,
  so queryset `delete()` reaches it. `JobsMixin.delete()` runs its job deletion and the collector
  in one transaction (`NB/netbox/models/features.py:513-520`), so the refusal rolls the job
  deletion back.

### Model decisions

| Model | Branchable | Reason |
|---|---|---|
| `Server` | No (resolver) | One row per pk: the Kea client and the Redis keys use main's current connection fields; sync never copies censored passwords into a branch |
| `SyncConfig` | No (plain model) | Global singleton |
| `KeaDhcpLink` | No (plain model, resolver) | Its only foreign keys go to `Server` (main-only) and `ContentType` (exempt) |
| ADR 0006 ownership link (future) | Yes, by the rule | `CASCADE` keys to IPAddress, Prefix, IPRange. Its revert and sync behaviour is designed with it; guard 2 blocks it until then |

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
  before any merge (round 1, N1). Merge replays the Tag delete in main. A revert of that merge
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
2. Branchability pin: compute the rule from `_meta` for every plugin model, assert
   `supports_branching()` agrees, and assert the branchable set equals a pinned empty set. The
   message says that a change needs a design decision, because open branches lack the table.
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
  (`views/reservation_mutations.py:216-247`); harmless under this design, but a GET should not.

### Increments

1. Resolver, `sync_vrf` `PROTECT`, the CI job, guards 2 and 4. Done when a provisioned branch holds
   no `netbox_kea_*` table and a Server edited in main after branch creation reads with main's
   values in the branch; both tests fail with the resolver removed. The VRF `PROTECT` test passes.
2. `SyncConfig` seeding.
3. Middleware (branch refusal, stale-selector predicate, sources header), banner, IPAddress panel,
   guard 1.
4. `KeaCommand` transport refactor with the client binding, guard 3; model receivers; job guard.

## Next action

Ratified. ADR 0007 records the decision. Implement in the increments above, starting with
increment 1: the resolver, `Server.sync_vrf` `PROTECT`, the branching CI job, and guards 2 and 4.
Done when a provisioned branch holds no `netbox_kea_*` table and a Server edited in main after
branch creation reads with main's values in the branch, and both tests fail with the resolver
removed. Tracking issue: #231.
