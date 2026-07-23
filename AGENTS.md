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
uv run ruff check netbox_kea/              # lint
uv run ruff format --check netbox_kea/     # check formatting
uv run ruff format netbox_kea/             # auto-format
uv run reuse lint                          # SPDX/REUSE compliance
uv run pre-commit install --install-hooks  # install pre-commit hooks (incl. pre-push opengrep)
./scripts/opengrep-scan.sh                 # custom opengrep ruleset gate (pre-push + CI)
./scripts/opengrep-test.sh                 # opengrep rule tests
```

### Unit tests (`netbox_kea/tests/`)

`testpaths` in `pyproject.toml` defaults to `netbox_kea/tests`. Unit tests run
against a **real NetBox install and a PostgreSQL test database** — NetBox requires
PostgreSQL (array/JSON fields, etc.); SQLite is not used. They do **not** need the
Kea/integration Docker stack: every Kea HTTP call is stubbed at the transport
boundary (see "Testing philosophy" below).

```bash
uv run pytest                                              # run all unit tests
uv run pytest netbox_kea/tests/test_views_leases.py -v     # single file
uv run pytest netbox_kea/tests/test_jobs.py::TestClass::test_method -v  # single test
```

`pythonpath` is set to `/opt/netbox/netbox` and `DJANGO_SETTINGS_MODULE=netbox.settings`
— unit tests require a NetBox installation at that path (present in the devcontainer).

### Integration tests (`tests/`, Docker required)

```bash
./tests/test_setup.sh   # generates TLS certs, builds a wheel, starts the compose stack
uv run pytest tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml
```

The compose stack runs: NetBox, netbox-worker, postgres, redis, nginx (basic-auth
+ TLS), and **kea-dhcp4 / kea-dhcp6 as direct daemons** (no Control Agent). The
daemons run with `-X` because Kea 3.2.0 refuses to start with an unsecured HTTP
control socket; the sockets are loopback-bound with nginx terminating auth in front.

### E2E tests

Playwright end-to-end tests live in `e2e/` and are separate from both unit and
integration tests.

### CI

- **Unit-test job**: pinned to NetBox **v4.6.4** (matches the devcontainer). Keep
  this in lockstep with `netbox_kea/tests/query_counts.json` (see "Query-count
  baselines") — bump the pin and re-record the baselines together.
- **Compatibility matrix**: runs the integration suite (`test_setup.sh`) against
  NetBox v4.3 (floor), v4.6 (ceiling), and the dev snapshot (allowed to fail).
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
  `has_control_agent`, per-server IPAM sync toggles, `sync_vrf` (FK to `ipam.VRF`;
  blank = global table), and `persist_config`. `clean()` runs a **live
  `version-get` connectivity check** per enabled service before saving.
  `get_client(version=4|6|None)` returns a protocol-aware `KeaClient`.
- **`SyncConfig` model** (`models.py`): singleton (pk=1) for global sync settings —
  `interval_minutes`, `sync_enabled` (global kill-switch), type toggles,
  `backfill_applied`. `SyncConfig.get(default_interval)` handles first-boot creation
  and a one-time PLUGINS_CONFIG backfill; once `backfill_applied=True` it never
  overrides UI changes. Plain `models.Model` (not a `NetBoxModel`).
- **`KeaClient`** (`kea.py`): wraps a `requests.Session`. All API calls go through
  `.command(command, service, arguments, check)`, which POSTs JSON to the
  **configured endpoint URL** (`self.url` — the daemon's `/` control socket, or a
  Control Agent URL). Responses are `list[KeaResponse]` (one entry per targeted
  service); `check_response()` raises `KeaException` if any result code is not in
  `check`. `.clone()` creates a thread-safe copy (fresh `requests.Session`) for
  concurrent lookups. **`send_service`**: `command()` includes the `service`
  argument only when the server is fronted by a Control Agent
  (`send_service = has_control_agent`); a direct daemon drops it, because Kea 3.2.0+
  rejects a `service` that does not match the daemon the request lands on.
- **`sync.py`**: bridges Kea data to NetBox IPAM — `sync_lease_to_netbox()`,
  `sync_reservation_to_netbox()`, `cleanup_stale_ips_batch()` (grouped by
  `(hostname, address_family)`). Raises `PartialPersistError` on partial failures.
- **`jobs.py`**: `KeaIpamSyncJob` (`@system_job`). Iterates all `Server` objects,
  runs subnet/lease/reservation/prefix/range sync phases, writes a per-server
  summary to the job log.
- **`__init__.py`**: `ready()` calls `_configure_sync_job_interval()` (patches the
  in-memory RQ registry from PLUGINS_CONFIG — no DB access, safe at image build).
  Ghost-job healing runs inside `KeaIpamSyncJob.enqueue_once()`, not `ready()`.
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
 └── KeaException                  # base — any non-ok result from Kea
      ├── KeaConfigTestError       # config-test failed
      ├── KeaConfigPersistError    # config-set / config-write failed
      │    └── PartialPersistError # config-set succeeded but config-write failed
      │         └── AmbiguousConfigSetError  # config-set status is ambiguous
      └── (generic Kea errors)
```

**Catch order matters**: always catch `PartialPersistError` *before* `KeaException`.
Order: `AmbiguousConfigSetError` → `PartialPersistError` → `KeaException`.

## Security & Code Quality Rules

Several of these are machine-enforced by the custom opengrep ruleset in
`.opengrep/kea-rules.yaml` (see `.opengrep/README.md`) — run on pre-push and in CI,
in addition to CodeRabbit's default opengrep packs. When a rule below has a matching
opengrep rule, a violation fails the gate before review.

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
  hook-related errors (result=2). Always catch `PartialPersistError` first.
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
- **Mock-discipline gate.** `netbox_kea/tests/mock_discipline.py` (+
  `test_mock_discipline.py`, a pre-commit hook) flags new spec-less
  `MagicMock`/`Mock`. Use `spec=` or a `# mock-ok` justification for the rare
  legitimate boundary (job-runner stand-in, error injection the real transport can't
  produce, an unreachable defensive guard).
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
  `UPDATE_QUERY_COUNTS=1 uv run pytest ...` (serially), then commit the file. The
  counts are tied to the NetBox version the unit-test CI pins (v4.6.4) — bump the pin
  and re-record together.
- **When fixing a bug, write the failing (red) test first**, confirm it fails against
  the unfixed code, then fix until green.

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
  intentional/existing); inner `Meta.ip_version` drives validation. CSV form fields
  (`dns_servers`, `ntp_servers`) need `clean_<field>` methods that split on commas,
  strip, drop empties, and rejoin.
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
- **Pool operations** support both Kea 2.x and 3.x APIs.

## Conventions

- **Commit messages**: Conventional Commits (feat, fix, docs, style, refactor, perf,
  test, build, ci, chore, revert) — enforced by a pre-commit hook.
- **REUSE/SPDX**: every file needs licensing. Source files carry inline SPDX headers;
  docs and generated files are annotated in `REUSE.toml`. `uv run reuse lint` must pass.
- **Ruff**: line length 120, max complexity 15, migrations excluded, E501 ignored,
  docstrings required except in tests/migrations/`__init__.py`.
