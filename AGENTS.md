<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# AGENTS.md — netbox-kea-ng

Guidance for AI coding agents (and humans) working in this repository. This is the
single source of truth for repo conventions; `CLAUDE.md` points here.

A NetBox plugin that integrates [Kea DHCP](https://www.isc.org/kea/) server
management. Published to PyPI as **`netbox-kea-ng`** — a fork of
[netbox-kea](https://github.com/devon-mar/netbox-kea) by Devon Mar. The Django
app/module name remains `netbox_kea` (unchanged from upstream). It exposes a
`Server` model representing a Kea endpoint, with views for live daemon status,
lease search/add/edit/delete, host-reservation CRUD, subnet/pool/shared-network
management, DHCP option editing, and automatic Kea→NetBox IPAM sync via a
background job.

**Kea connection model.** Kea 3.0 removed the Control Agent: each DHCP daemon
exposes its own HTTP control socket, so the plugin connects **directly** to each
daemon (`has_control_agent=False`, the modern default for Kea 3.0+). A legacy
Control Agent (Kea < 3.0, or a per-protocol CA) is still supported via
`has_control_agent=True`. This flag is the single source of truth for request
routing — see "Protocol-aware / direct-daemon client" below.

## Build, Test & Lint

```bash
uv sync                                    # install dev dependencies (activates .venv via .envrc)
uv build                                   # build wheel (required before integration tests)
uv run ruff check .                        # lint
uv run ruff format --check .               # check formatting
uv run ruff format .                       # auto-format
uv run reuse lint                          # SPDX/REUSE compliance
uv run --native-tls pre-commit install --install-hooks  # install configured hook types
./scripts/opengrep-scan.sh                 # custom opengrep ruleset gate (pre-commit only)
./scripts/opengrep-test.sh                 # opengrep rule tests
uv run ./scripts/mypy-gate.sh              # type check, new errors only (pre-push + CI)
uv run ./scripts/mypy-gate.sh --sync       # rewrite mypy-baseline.txt after fixing errors
```

### Unit tests (`netbox_kea/tests/`)

`testpaths` in `pyproject.toml` defaults to `netbox_kea/tests`. Unit tests run
against a **real NetBox install and a PostgreSQL test database** — NetBox requires
PostgreSQL (array/JSON fields, etc.); SQLite is not used. They do **not** need the
Kea/integration Docker stack: every Kea HTTP call is stubbed at the transport
boundary (see "Testing philosophy" below).

Use a task-specific PostgreSQL database and a dedicated Redis host. Each xdist
worker gets a private PostgreSQL database and private Redis task and cache
databases. Eight workers is the supported maximum. `-n auto` is capped there by
`pytest_xdist_auto_num_workers` in `netbox_kea/tests/conftest.py`, so it is safe on
any machine. Always use xdist, including for focused tests. A serial run can clear
Redis database 1, which the shared manual verification environment uses as its
default cache.

`TEST_DB_NAME` must start with `test_`; `TEST_REDIS_HOST` names a Redis reachable
from the run. Both are required. The values below are recommended local defaults. CI
sets its own database name for each job. Change them to something only this task holds
when another project shares the host: a shared database is rebuilt by whichever suite
runs `--create-db` next, and a shared Redis is written to by every worker.

```bash
TEST_DB_NAME=test_netbox_kea_local TEST_REDIS_HOST=localhost \
  uv run --native-tls pytest --reuse-db -n auto --maxschedchunk=1
TEST_DB_NAME=test_netbox_kea_local TEST_REDIS_HOST=localhost \
  uv run --native-tls pytest --reuse-db -n auto --maxschedchunk=1 netbox_kea/tests/test_views_leases.py -v
```

`pythonpath` is set to `/opt/netbox/netbox` and
`DJANGO_SETTINGS_MODULE=netbox_kea.tests.isolated_settings`
— unit tests require a NetBox installation at that path (present in the devcontainer).

### Integration tests (`tests/`, Docker required)

```bash
./tests/test_setup.sh   # generates TLS certs, builds a wheel, starts the compose stack
uv run --native-tls pytest -p no:django tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml
```

The `host_ca` build secret defaults to `/etc/ssl/certs/ca-certificates.crt`. Compose
reads that path before the build starts, so set `SSL_CERT_FILE` to the local CA bundle
on any host that keeps it elsewhere (for example `/etc/pki/tls/certs/ca-bundle.crt`).

The compose stack runs: NetBox, netbox-worker, postgres, redis, nginx (basic-auth
+ TLS), and **kea-dhcp4 / kea-dhcp6 as direct daemons** (no Control Agent). The
daemons run with `-X` because Kea 3.2.0 refuses to start with an unsecured HTTP
control socket; the sockets are loopback-bound with nginx terminating auth in front.

### Browser tests (`tests/ui/`, Docker required)

Playwright tests are part of the integration suite: one run over `tests/` covers both,
against the same compose stack. `tests/ui/conftest.py` holds the one browser harness
both modules share: login, the Server object, a real `KeaClient` on each daemon's
loopback socket, and HTTP-error tracking. `test_ui.py` covers tables,
search, and permissions; `test_workflows.py` covers navigation, badge enrichment, and
CRUD lifecycles.

They used to live in a top-level `e2e/` directory that no workflow named, so they never
ran anywhere. `test_pytest_configuration.py` now asserts the suite stays inside the path
the integration job executes.

`test_branching_refusal.py` needs the netbox-branching variant of the harness: set
`COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.branching.yml` and
`NETBOX_BRANCHING_VERSION` (the value in the `env` of `.github/workflows/ci.yml`) before
`test_setup.sh`, with a NetBox 4.7 image. Elsewhere it skips; `NETBOX_KEA_REQUIRE_BRANCHING=1`
makes it fail instead.

### CI

- **Unit-test job**: pinned to the exact NetBox patch release named by
  `QUERY_COUNT_NETBOX_VERSION` in `netbox_kea/tests/conftest.py`, because
  `netbox_kea/tests/query_counts.json` describes that release only (see "Query-count
  baselines"). Bump the constant, `NETBOX_RELEASE` in the workflow `env`, and the baselines in one
  change.
- **Branching job**: the unit-test NetBox release with netbox-branching 1.2.1 and netbox-plugin-dhcp 0.2.0.
  It runs `test_branching.py` with `NETBOX_KEA_REQUIRE_BRANCHING=1`, so the module fails
  instead of skipping when netbox-branching is absent. `netbox_kea/branching.py` is the only
  module that imports `netbox_branching` (ADR 0007, `docs/design/netbox-branching.md`).
  Every migration sets `fake_on_branch`; guard 4 in `test_branching.py` checks the value.
  `BranchRefusalMiddleware` in `branching.py` refuses every unsafe request to a netbox_kea URL
  callback in a branch, or with an unusable branch selection. Below the middleware, `KeaClient.command()`
  refuses a `write` member of `KeaCommand` with `BranchActive` (`Server.get_client()` binds the client
  to the active branch, and `clone()` keeps the binding), `pre_save` and `pre_delete` receivers in
  `branching.py` refuse a save or a delete of every netbox_kea row, and `KeaIpamSyncJob` fails before
  any read. Guard 1 in `test_branching.py`
  sends GET, HEAD, OPTIONS, POST, PUT, PATCH and DELETE to every netbox_kea URL in a provisioned
  branch, API action routes included; a new route with a parameter the guard cannot build fails
  by name, so teach `_route_arguments` the object.
- **Compatibility matrix**: runs the integration suite (`test_setup.sh`) against
  NetBox v4.3 (floor), v4.7 (ceiling), and the dev snapshot (allowed to fail).
- **Branching browser job**: the integration steps on the netbox-branching variant of the
  harness (a NetBox 4.7 image), running only `tests/ui/test_branching_refusal.py`.
- Playwright traces on failure are uploaded as artifacts.

Ruff is configured in `pyproject.toml`: line length 120, max complexity 15,
migrations excluded, E501 ignored (handled by the formatter). Docstrings required
except in tests, migrations, and `__init__.py`.

## Architecture

```text
URL request
  → urls.py             (routes to view classes)
  → views/              (view modules; each calls server.get_client() → KeaClient)
      _base.py          (ConditionalLoginRequiredMixin, _KeaChangeMixin, shared helpers)
      server.py         (Server CRUD, status tab)
      leases.py         (DHCPv4/v6 lease search, add, edit, delete, badge enrichment)
      reservations.py   (DHCPv4/v6 reservation CRUD)
      subnets.py        (subnet/pool management)
      shared_networks.py (shared network CRUD)
      options.py        (global and per-subnet DHCP option editing)
      dhcp_control.py   (enable/disable DHCP daemons)
      combined.py       (cross-server dashboard, leases, reservations, subnets)
      sync_views.py     (per-server IPAM sync UI)
      sync_jobs.py      (jobs tab, periodic sync management, SyncConfig admin)
  → config_write.py     (Configuration Changes: typed outcome, advisory lock, persist step)
  → kea.py              (HTTP POST to each daemon's / control socket)
  → sync.py             (bridges Kea data to NetBox IPAM)
  → jobs.py             (KeaIpamSyncJob — periodic background sync)
  → tables.py           (non-model GenericTable renders enriched dicts)
  → template            (django-tables2 + HTMX for pagination)
```

### Core components

- **`Server` model** (`models.py`): the only persisted model. Stores connection
  config: `ca_url` (default/fallback endpoint), optional per-protocol `dhcp4_url` /
  `dhcp6_url` (dual-URL mode), CA and per-protocol credentials, TLS fields
  (`ssl_verify`, `ca_file_path`, `client_cert_path`, `client_key_path`),
  `has_control_agent`, per-server IPAM sync toggles, `sync_vrf` (`PROTECT` FK to `ipam.VRF`;
  blank = global table), and `persist_config`. `clean()` runs a **live
  `version-get` connectivity check** per enabled service before saving.
  `get_client(version=4|6|None)` returns a protocol-aware `KeaClient`.
- **`SyncConfig` model** (`models.py`): singleton (pk=1) for global sync settings —
  `interval_minutes`, `sync_enabled` (global kill-switch), type toggles. Migration 0018
  creates the row from PLUGINS_CONFIG and applies the one-time backfill, so
  `SyncConfig.get()` only reads it. A TransactionTestCase flush deletes the row, and a
  `post_migrate` receiver in `netbox_kea/tests/conftest.py` creates it again with the migration's
  values. Plain `models.Model` (not a `NetBoxModel`).
- **`KeaClient`** (`kea.py`): wraps a `requests.Session`. All API calls go through
  `.command(command, target, arguments, check)`, the only HTTP send of the plugin, which POSTs
  JSON to the **configured endpoint URL** (`self.url`: the daemon's `/` control socket, or a
  Control Agent URL). `command` is a `KeaCommand` member, never a string (a string is a
  `TypeError`); each member has a `read` or `write` kind. `target` is the `Family` (4 or 6), or
  `None` for the Control Agent itself. A family-specific command comes from a per-family mapping
  in `kea.py`, such as `SUBNET_LIST[family]`; never format a command name. A new member needs an
  entry in the pinned read or write set of `test_kea_command.py`. `write_guard` is a required
  keyword argument: `Server.get_client()` passes the branch binding, and unit tests build clients
  through `kea_stub.kea_client()`, which passes it too. Responses are
  `list[KeaResponse]`; `check_response()` raises `KeaException` if any result code is not in
  `check`. `.clone()` creates a thread-safe copy (fresh `requests.Session`) for
  concurrent lookups. **`send_service`**: `command()` sends the target as the `service`
  argument only when the server is fronted by a Control Agent
  (`send_service = has_control_agent`); a direct daemon drops it, because Kea 3.2.0+
  rejects a `service` that does not match the daemon the request lands on.
- **`sync.py`**: bridges Kea data to NetBox IPAM — `sync_lease_to_netbox()`,
  `sync_reservation_to_netbox()`, `cleanup_stale_ips_batch()` (grouped by
  `(hostname, address_family)`). Raises `DuplicateNetBoxRowsError` when more than one
  NetBox Prefix or IP Range matches one Kea Subnet or Pool.
- **`jobs.py`**: `KeaIpamSyncJob` (`@system_job`). Iterates all `Server` objects,
  runs subnet/lease/reservation/prefix/range sync phases, writes a per-server
  summary to the job log.
- **Sync interval**: `SyncConfig.interval_minutes` is the only runtime source.
  `KeaIpamSyncJob.enqueue_once()` (rqworker startup) and each periodic `run()` read it;
  PLUGINS_CONFIG `sync_interval_minutes` only seeds the row in migration 0018.
  Ghost-job healing also runs inside `enqueue_once()`, not `ready()`.
- **REST API** (`api/`): `NetBoxModelViewSet` + `NetBoxModelSerializer` — only the
  `Server` model is exposed. All password fields are write-only.
- **GraphQL** (`graphql.py`): a strawberry-django `ServerType` + `Query`
  (`server` / `server_list` fields), auto-discovered by NetBox. Note the **legacy
  single-module layout** (`graphql.py`, not a `graphql/` package with `types.py`),
  so NetBox's standard `APIViewTestCases.GraphQLTestCase` cannot resolve
  `netbox_kea.graphql.types.ServerType`; the Server API tests compose the REST CRUD
  mixins and leave GraphQL out until the schema moves to the package layout.

### Exception hierarchy

```text
Exception
 ├── KeaException                  # base: any non-ok result from Kea
 └── ConfigChangeRejected          # config_write: the change is not live, with a reason
```

**`config_write` owns every Configuration Change** (ADR 0005). An
operation returns a `ConfigChangeOutcome` (`applied`/`unknown` and
`persisted`/`failed`/`not-requested`) or raises `ConfigChangeRejected` with a reason.
A view runs it through `_run_config_change` in `views/_base.py`, which owns the
messages, and catches nothing itself. Shared Network add, edit and delete, Subnet add, edit and
delete, Pool add and delete, Subnet and server DHCP Options, and Option Definition add and
delete use it. An OpenGrep rule refuses an `except` of `ConfigChangeRejected` in `views/`
outside `_run_config_change`. A second rule keeps that function in `views/_base.py`, and a third
refuses a broad `except` around a direct `config_write` call in a view. Only `config_write` operations take the per-daemon advisory
lock. Reservation mutations call `KeaClient.persist`, but they do not wait for the lock. A Subnet, Pool
or Subnet DHCP Options operation takes the Subnet ID and the CIDR that the page showed, and
sends nothing unless its `MutationScope` returns a Verified Subnet with both.

Subnet add is the first multi-step change: `add_subnet` adds the Subnet and then assigns it to
a Shared Network. When the assignment does not apply, it deletes the Subnet again, but only
while a fresh `subnet{v}-list` read shows the Subnet with the sent ID and CIDR and no Shared Network. The
persist step runs once, at the end. In tests, `SubnetDaemon` in `kea_stub.py` holds Subnets
and Shared Networks, so a test can script a Kea failure or a change by another writer.

Subnet edit (`edit_subnet`) removes the Subnet from its Shared Network, adds it to the new one,
and then updates the fields, because Kea refuses to add a Subnet that is already in a Shared
Network. It also takes the Shared Network that the page showed, and sends nothing while the
scope shows another one; a page that could not confirm the membership cannot save. When a step
does not apply, the operation undoes the applied membership steps, newest first, each only while
a fresh `subnet{v}-list` read shows the membership that the step set. The field update is the last step, so a
rollback never undoes it. Both operations run their steps through `_run_steps` in `config_write`.

A check after a step reads only what it compares, while the operation holds the lock: identity and
membership come from one `subnet{v}-list` read (`subnet_catalogue.read_identity`), not from a full scope with
`config-get`. A Pool check still reads a fresh scope. The target Shared Network of a move or an assignment comes
from the `ServerConfigurationSnapshot` that the operation's own scope read (`MutationScope.has_shared_network`).
When that read cannot show the Shared Network, the operation sends `network{v}-get` as before.

Subnet edit and Shared Network edit also take the values that the page showed (`shown`, the type of the edit),
and send nothing while a live value differs, because a save writes back every field that the form shows. One
function maps the live facts to those values for the GET and for the check under the lock:
`server_configuration.shown_subnet` and `shown_shared_network`. `_ShownValuesForm` builds each hidden `shown_`
field as a copy of its visible field, so a value that Kea writes in another text form is not a change.

A read-modify-write operation holds the lock from its `config-get` to the end of the
persist step. `KeaClient` sends each command (`config_candidate`, `config_test`,
`config_set`); `CandidateConfiguration` in `kea.py` edits the raw configuration in place,
because the wire-discipline gate keeps wire literals out of `config_write`. An edit
raises `MalformedConfiguration` for a configuration that it cannot edit safely, and
`config_write` catches only that type, so a bug still fails loudly. Any failure result on
`config-set` is `unknown`, because Kea commits the configuration before the hook
initialization can fail.
A stale or renamed DHCP Option row (`DHCPOptionConflict`, `DHCPOptionNameChange`) is not
a Configuration Change result: it leaves the operation before any command that changes
the configuration, and the options views show it as a form error.

## Security & Code Quality Rules

Several of these are machine-enforced by the custom opengrep ruleset in
`.opengrep/kea-rules.yaml` (see `.opengrep/README.md`). The pre-commit hook runs
these rules locally. Keep OpenGrep out of CI so CodeRabbit can run its own analysis.
When a rule below has a matching opengrep rule, a violation fails the local hook.

- **Never leak exception details to HTTP responses.** Use `logger.exception()`
  server-side and return a generic message like `"An internal error occurred"`.
  Raw `str(exc)` can expose internal URLs, TLS details, or Kea config.
- **Always pass `version=` to `server.get_client()`** when the DHCP version is known
  (`server.get_client(version=self.dhcp_version)`). Omitting it falls back to
  `ca_url` even when a protocol-specific URL is configured.
- **Always call `server.get_client()` inside a try block** — client creation can
  raise `ValueError` / `requests.RequestException` on bad config or connectivity.
  Never call it at module/class level or before error handling is in scope.
- **Validate Kea response shape before indexing.** After `client.command()`, check
  `resp` is a non-empty list and `resp[0]` is a dict before reading
  `resp[0]["arguments"]`; check nested keys (`"leases"`, `"subnet4"`, …) are lists
  before indexing. Malformed payloads should raise `RuntimeError` to hit existing
  handlers.
- **Catch `(KeaException, requests.RequestException, ValueError)` consistently** in
  mutation handlers. Split `KeaException` when you need `kea_error_hint(exc)` for
  hook-related errors (result=2). A Configuration Change goes through `_run_config_change`
  instead (see "Exception hierarchy").
- **Use `kea_error_hint(exc)` for user-facing Kea error messages** — it maps result
  codes to actionable hints (result=2 → hook library not loaded, etc.).
- **Guard action URLs/buttons by permission AND lookup state.** Don't offer
  Sync/Reserve for leases whose reservation lookup failed (check `failed_ips` /
  `failed_mac_keys`); don't offer add/edit to users without `change` permission.
- **Django form querysets must be evaluated at instantiation, not at class-definition
  time.** Set `self.fields["field"].queryset` in `__init__` — class-level querysets
  go stale in long-running processes.
- **django-tables2 Column instances must not be shared across table classes.** Use a
  factory function, not a module-level instance.
- **Catch `DatabaseError` (not just `ProgrammingError`/`OperationalError`)** around
  non-critical DB writes (e.g. `JournalEntry.objects.create`) so a successful Kea
  operation never turns into a 500.
- **DHCPv6 reservations use `ip-addresses` (list), not `ip-address` (string).** Check
  both fields when inspecting reservation data.
- **Pass `timeout=` to every `subprocess` call**, in the package, `scripts/`, and the tests.
  A child process that stops responding must fail the caller, not hang it.

## Testing philosophy

Value tests by how much real behaviour they exercise: **end-to-end → integration
against real deps (real DB/ORM/serializers/forms) → narrow unit**. Mocks are a last
resort, reserved for true external boundaries you cannot run locally.

- **Stub the HTTP boundary, drive the real `KeaClient`.** Unit tests do **not** mock
  `KeaClient` or patch `netbox_kea.models.KeaClient`. They construct a real client and
  stub only the transport by patching `netbox_kea.kea.requests.Session.post` — the
  `stub_kea()` context manager in `netbox_kea/tests/kea_stub.py`. This exercises the
  real `command()` payload building, response parsing, and error handling, so a broken
  parser can't hide behind a `MagicMock`. Register responses by command name (dict /
  list / `queued(...)` / a `(body) -> payload` callable / an exception instance raised
  at the boundary). Patching is at the class level so it also covers `clone()`.
- **Recorded Kea replies check the parser.** A hand-written stub shows what we expect
  Kea to return. `netbox_kea/tests/kea_recordings/` holds `config-get` and
  `subnet{4,6}-list` replies recorded from a real Kea (the harness `KEA_VERSION`), with
  coverage configurations that use every field `server_configuration` reads.
  `test_kea_recordings.py` requires zero diagnostics. The script also writes
  `accepted-keys.json` from the keyword tables that Kea's `config-test` and
  `config-set` check in the same release (`simple_parser{4,6}.cc`). `stub_kea()`
  fails a `config-test` or `config-set` whose Shared Network or Subnet carries a key
  outside that file, because Kea rejects unknown keys. When you bump `KEA_VERSION` or
  make the parser read a new field, add the field to `kea-dhcp{4,6}.conf` and run
  `scripts/record_kea_config_get.py` (Docker and curl required).
- **Kea command names come from a real Kea.** The script also records the
  `list-commands` reply of each daemon. The coverage configurations load the same hook
  libraries as `tests/docker/kea_configs/`. `WIRE_COMMANDS` in `kea_wire_discipline.py`
  is that recorded set, and `stub_kea()` fails a command outside it: registered, sent, or
  listed in a stubbed `list-commands` reply. Never add a command name by hand. The
  black-box test `tests/test_kea_commands.py` compares the live harness daemons with the
  recording in CI. When you change `KEA_VERSION` or the harness hook libraries, run the
  script again.
- **Type-check gate.** `scripts/mypy-gate.sh` (+ `test_mypy_gate.py`, a pre-push hook,
  the CI `lint` job) type-checks `netbox_kea/` and fails only on errors that are absent
  from `mypy-baseline.txt`. It exists to catch annotation drift between a producer and
  its consumer: a `list[dict | Reservation]` passed to a `list[Reservation]` parameter is
  an error because `list` is invariant, and that reached review once already. NetBox is
  deliberately **not** installed for this gate, so NetBox objects are `Any`; every
  first-party signature and call site is still checked, and no upstream NetBox release
  can break the gate. Unlike the mock-discipline baseline, `mypy-baseline.txt` is **not**
  empty: it records the errors that already existed when the gate was added. Fix one and
  run `--sync` to shrink it; never `--sync` to silence a new error. The baseline matches
  on error text, not line number, so edits above an error do not force a resync.
  **Always run the gate through `uv run`**, so the locked dev group is active. `django-stubs`
  is a dev dependency, and mypy resolves Django imports through its stubs only when it is
  installed; a baseline generated without it misses ~95 findings and CI then fails on a
  clean checkout. A resolved baseline error does not fail the gate (`--allow-unsynced`),
  so fixing types never breaks the build, and a mypy exit status above 1 fails loudly
  rather than filtering an empty report into a false pass.

- **Mock-discipline gate.** `netbox_kea/tests/mock_discipline.py` (+
  `test_mock_discipline.py`, a pre-commit hook) flags new spec-less
  `MagicMock`/`Mock`, and new `patch("netbox_kea…")` / `patch.object(<our class>, …)`
  without `autospec=` — `patch` returns the same fabricating `MagicMock`, and an
  unspecced one also survives a signature change in the function it replaces.
  Patching a real boundary (`requests.Session.post`, `django_rq`) is not flagged.
  Use `spec=`/`autospec=True` or a `# mock-ok` justification for the rare legitimate
  boundary (job-runner stand-in, error injection the real transport can't produce, an
  unreachable defensive guard). `mock_discipline_baseline.txt` is **empty** and must stay
  that way: it grandfathers accepted violations per (file, function), so regenerating it
  to silence a failure defeats the gate. Fix the call site instead.
- **Kea wire-discipline gate.** `netbox_kea/tests/kea_wire_discipline.py` checks
  production code for Kea command names, hyphenated payload keys, and family-suffixed
  configuration keys or service names. String templates (f-strings, `.format`, `%`, `+`)
  count when they can build a wire literal. Wire owners are `kea.py`, `server_configuration.py`,
  `subnet_catalogue.py`, `reservations.py`, and `dhcp_options.py`, relative to `netbox_kea/`.
  The checker excludes these exact modules, tests, and migrations. The transport stub
  `tests/kea_stub.py` may also use wire literals to model Kea responses.
  It also checks `arguments` when code uses it as a raw payload key. Prefer typed domain
  interfaces when the gate fails. The baseline is the follow-up brief and only shrinks.
  Use `--update-baseline` to record decreases. It refuses new sites and higher counts
  without changing the baseline. A test prevents the baseline from adding files.
  The pre-commit hook and the real-tree suite test enforce the budgets.
- **Standard NetBox model coverage via mixins.** For the `Server` model (a
  `NetBoxModel` with standard generic views + `NetBoxModelViewSet`), use NetBox's
  `ViewTestCases` / `APIViewTestCases` (see `test_server_generic.py`). Wire plugin
  namespaces: UI `_get_base_url` → `plugins:netbox_kea:server_{}`; API
  `view_namespace = "plugins-api:netbox_kea"`. `Server.clean()`'s live check (and the
  REST serializer's `full_clean()`) are answered by `stub_kea({"version-get": ...})`
  in `setUp`; build fixtures with `bulk_create` (skips `Model.clean()`). These mixins
  don't fit the Kea-proxy views (leases/subnets/reservations over live daemon data) —
  those stay `stub_kea`-driven.
- **Query-count baselines.** The list-view mixins assert an exact SQL query count
  against `netbox_kea/tests/query_counts.json` to catch N+1 drift. Record/update with
  `TEST_DB_NAME=test_netbox_kea_local TEST_REDIS_HOST=localhost \
  UPDATE_QUERY_COUNTS=1 uv run --native-tls pytest -n 1 ...`, then commit the file. One xdist
  worker prevents concurrent writes and keeps Redis isolated. A count is a fact about
  one NetBox release, not about the plugin alone: NetBox re-records its own baselines on
  patch releases. `QUERY_COUNT_NETBOX_VERSION` in `netbox_kea/tests/conftest.py` names
  the release the file describes, the unit-test CI job pins that same release, and the
  assertions are skipped with a warning on any other release. To move to a new NetBox,
  bump the constant, bump `NETBOX_RELEASE` in the workflow `env`, and re-record in one change.
- **When fixing a bug, write the failing (red) test first**, confirm it fails against
  the unfixed code, then fix until green.
- **No source line numbers in comments or docstrings**: name the function or the
  behaviour. `test_no_source_line_references.py` fails on each "Lines 731-736" or "(line 910)".

### Unit test seams & patterns

- **User model**: use `get_user_model()`, never `from django.contrib.auth.models import User`.
- **API auth in tests**: `api_client.force_authenticate(user=self.user)` (NetBox v4
  tokens use the `nbt_` format).
- **BulkImportView POST**: requires `data=`, `format='csv'`, `csv_delimiter=','`.
- **DB-less helper tests**: `SimpleTestCase` is fine for pure logic; for anything that
  touches `get_client()` add `@override_settings(PLUGINS_CONFIG=...)` so `kea_timeout`
  resolves.

## Key Patterns

- **Non-model tables**: lease/subnet/reservation/shared-network tables use
  `GenericTable(BaseTable)` (no Django model). They accept `list[dict]` and define
  `objects_count` = `len(self.data)` for NetBox pagination.
- **HTMX pagination**: lease views serve a full page or an HTMX partial from the same
  `get()` via `htmx_partial(request, ...)`. The hidden `page` field uses
  `VeryHiddenInput` (renders empty) to avoid form conflicts.
- **View registration**: standard CRUD uses `@register_model_view(Server)` /
  `@register_model_view(Server, "edit")`; `get_model_urls("netbox_kea", "server")`
  auto-generates detail/edit/delete/changelog/journal. Custom routes (leases,
  reservations, subnets, shared-networks, options, DHCP control) are declared
  explicitly before the `include()`. Custom tabs use `OptionalViewTab` (a `ViewTab`
  accepting `is_enabled: Callable[[Server], bool]`).
- **Generic views with TypeVar**: `BaseServerLeasesView` is `generic.ObjectView,
  Generic[T]` (T bound to `BaseTable`); concrete subclasses declare `table_class`,
  `form_class`, `dhcp_version`, `lease_service`. The base handles pagination, HTMX,
  search, export, delete routing.
- **FakeLeaseModel**: leases aren't a real model, so `FakeLeaseModel` /
  `FakeLeaseModelMeta` provide `app_label`/`model_name` so `GetReturnURLMixin`
  resolves in delete views.
- **Lease badge enrichment**: `_enrich_leases_with_badges()` runs a two-phase
  reservation lookup (IP-based, then MAC-based for the misses) using
  `ThreadPoolExecutor` with `client.clone()` workers; composite `(mac, subnet_id)`
  keys dedupe and track failures; the `_FETCH_ERROR` sentinel distinguishes lookup
  errors from genuine not-found.
- **Sync lifecycle**: IP status is `dhcp` (dynamic lease only), `reserved`
  (reservation only), or `active` (both). `cleanup_stale_ips_batch()` groups by
  `(hostname, address_family)`; cleanup is skipped when errors > 0. Single-sync paths
  (`_sync()`) use `cleanup=False` (a one-record sync has no complete keep-set).
- **Kea option aliases**: DNS options can be `domain-name-servers` or `dns-servers`;
  NTP can be `ntp-servers` or `sntp-servers`. Search both alias tuples.
- **Forms**: lease search forms inherit `BaseLeasesSarchForm` (the typo is
  intentional/existing); inner `Meta.ip_version` drives validation. The Subnet and
  Shared Network forms clean in the field class (`_AddressListField` for `dns_servers`,
  `ntp_servers` and relay addresses gives canonical addresses), so a copy of a field
  cleans the same way. A change form that refuses stale values inherits
  `_ShownValuesForm` and names its managed fields once, in `shown_names`.
  `dhcp_options.form_managed_entry` picks the one DHCP Option entry that a form field
  manages, for the display and for the save. Two fitting entries refuse the form.
- **API URL naming**: the serializer's `HyperlinkedIdentityField` uses
  `view_name="plugins-api:netbox_kea-api:server-detail"` — `plugins-api:` prefix and
  `-api:` namespace suffix are NetBox conventions.

## Kea API Reference

- **Primary**: `kea.readthedocs.io/en/latest/api.html` — full JSON command schemas.
- **Live discovery**: run `list-commands` against the target daemon/service to confirm
  which hook libraries are loaded; cache per request; show a warning banner in the UI
  when a required command is absent. Only set `hook_available=False` on result code 2.
- **Key hooks** and the commands they gate:
  - `host_cmds` — all `reservation-*` commands (open source since Kea 2.7.7 / MPL 2.0)
  - `lease_cmds` — `lease4/6-get-by-hostname/hw-address/state`, `lease4/6-update/add`
  - `subnet_cmds` — `subnet4/6-list/get/add/update` (alternative to `config-get`)
  - `stat_cmds` — `stat-lease4/6-get` for per-subnet utilization
- **Pool operations** use `subnet4/6-delta-add` and `subnet4/6-delta-del` (Kea 2.2+). `subnet_cmds` has no `subnet4/6-pool-add` or `-pool-del` command.

## Conventions

- **Commit messages**: Conventional Commits (feat, fix, docs, style, refactor, perf,
  test, build, ci, chore, revert) — enforced by a pre-commit hook.
- **REUSE/SPDX**: every file needs licensing; `uv run reuse lint` must pass. Each file
  carries an inline SPDX header, and a new file without one fails the lint. Templates use
  `{# ... #}` (an HTML comment renders into the page). `REUSE.toml` lists only files that
  cannot take a comment or are generated (license text, binary, JSON, lock file, changelog).
  Name one `SPDX-FileCopyrightText` line per author whose lines are in the file. Never
  remove a holder whose code is still in the file (Apache-2.0 section 4(c)).
- **Ruff**: line length 120, max complexity 15, migrations excluded, E501 ignored,
  docstrings required except in tests/migrations/`__init__.py`.
