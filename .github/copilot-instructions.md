# Copilot Instructions — netbox-kea-ng

A NetBox plugin that integrates [Kea DHCP](https://www.isc.org/kea/) server management. Published to PyPI as **`netbox-kea-ng`** — a fork of [netbox-kea](https://github.com/devon-mar/netbox-kea) by Devon Mar. The Django app/module name remains `netbox_kea` (unchanged from upstream). Exposes a `Server` model representing a Kea Control Agent endpoint, with views for live daemon status, lease search/delete/add/edit, host reservation CRUD, subnet/pool/shared-network management, and automatic Kea→NetBox IPAM sync via a background job.

## Build, Test & Lint

```bash
# Install dev dependencies (also activates .venv via .envrc if using direnv)
uv sync

# Lint / format
uv run ruff check netbox_kea/
uv run ruff format --check netbox_kea/
uv run ruff format netbox_kea/

# REUSE compliance
uv run reuse lint

# Build wheel (required before integration tests)
uv build

# Install pre-commit hooks
uv run pre-commit install
```

### Unit tests (no Docker required)

`testpaths` in `pyproject.toml` defaults to `netbox_kea/tests`. These tests mock all Kea HTTP calls and use SQLite.

```bash
uv run pytest                                                        # run all unit tests
uv run pytest netbox_kea/tests/test_views_leases.py -v               # single file
uv run pytest netbox_kea/tests/test_jobs.py::TestClass::test_method -v  # single test
```

Note: `pythonpath` is set to `/opt/netbox/netbox` and `DJANGO_SETTINGS_MODULE=netbox.settings` — unit tests require a NetBox installation at that path (present in devcontainer).

### Integration tests (Docker required)

```bash
./tests/test_setup.sh   # generates certs, builds Docker images, starts containers
uv run pytest tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml

# Single file / function
uv run pytest tests/test_netbox_kea_api_server.py -v
uv run pytest tests/test_ui.py::test_function_name -v
```

The compose stack includes: NetBox, netbox-worker, postgres, redis, nginx (basic auth + TLS), kea-ctrl-agent, kea-dhcp4, kea-dhcp6.

### E2E tests

Playwright end-to-end tests live in `e2e/` and are separate from both unit and integration tests.

CI tests against NetBox v4.0–v4.5 using a matrix build. Playwright traces on failure are uploaded as artifacts.

Ruff is configured in `pyproject.toml` — migrations are excluded from linting. Line length (E501) is ignored.

## Architecture

```text
URL request
  → urls.py             (routes to view classes)
  → views/              (view modules, all import from _base.py helpers)
      _base.py          (BaseTable, GenericTable, htmx_partial, OptionalViewTab, TypeVar views)
      server.py         (Server CRUD, status tab)
      leases.py         (DHCPv4/v6 lease search, delete, add, edit, bulk import)
      reservations.py   (DHCPv4/v6 reservation CRUD)
      subnets.py        (subnet/pool management)
      shared_networks.py (shared network CRUD)
      options.py        (global and per-subnet DHCP option editing)
      dhcp_control.py   (enable/disable DHCP daemons)
      combined.py       (cross-server dashboard, leases, reservations, subnets views)
      sync_views.py     (per-server IPAM sync config UI)
      sync_jobs.py      (jobs tab, periodic sync management, SyncConfig admin)
  → kea.py              (HTTP POST to Kea Control Agent /api/v1/)
  → sync.py             (bridges Kea data to NetBox IPAM)
  → jobs.py             (KeaIpamSyncJob — periodic background sync)
  → signals.py          (Django signals for sync-related events)
  → utilities.py        (format_leases, format_duration, helpers)
  → tables.py           (non-model GenericTable renders enriched dicts)
  → template            (rendered with django-tables2 + HTMX for pagination)
```

**`Server` model** (`models.py`) is the primary persisted model. Fields:
- `ca_url` — default/fallback Kea Control Agent URL
- `dhcp4_url` / `dhcp6_url` — optional per-protocol URLs (dual-URL mode)
- `has_control_agent` — when False, skips Control Agent status row in status view
- `ca_username` / `ca_password` — default credentials
- `dhcp4_username` / `dhcp4_password` / `dhcp6_username` / `dhcp6_password` — per-protocol credential overrides
- `dhcp4` / `dhcp6` — enable/disable per-protocol tabs
- `ssl_verify`, `ca_file_path`, `client_cert_path`, `client_key_path` — TLS config
- `sync_enabled`, `sync_leases_enabled`, `sync_reservations_enabled`, `sync_prefixes_enabled`, `sync_ip_ranges_enabled` — per-server IPAM sync toggles
- `sync_vrf` — FK to `ipam.VRF` for prefix/range sync (no global fallback; blank = global routing table)
- `persist_config` — auto-save Kea config after changes via `config-write`

`clean()` performs live connectivity checks before saving. `get_client(version=4|6|None)` returns a protocol-aware `KeaClient`.

**`SyncConfig` model** (`models.py`) — singleton (pk=1 always) for global sync settings:
- `interval_minutes`, `sync_enabled` (global kill-switch), four type-toggle fields, `backfill_applied`
- `SyncConfig.get(default_interval)` handles creation-on-first-boot and one-time PLUGINS_CONFIG backfill
- UI-editable; changes take effect without restart

**`KeaClient`** (`kea.py`) wraps a `requests.Session`. All API calls go through `.command(command, service, arguments)` which POSTs JSON to the Kea Control Agent. Responses are `list[KeaResponse]` — one entry per service. `check_response()` raises `KeaException` if any result code is not in `ok_codes`. `.clone()` creates a thread-safe copy for concurrent lookups.

**`sync.py`** bridges Kea data to NetBox IPAM: `sync_lease_to_netbox()` (status=active), `sync_reservation_to_netbox()` (status=reserved), `cleanup_stale_ips_batch()`. Handles `PartialPersistError` for partial failures.

**`jobs.py`** — `KeaIpamSyncJob` decorated with `@system_job(interval=_DEFAULT_INTERVAL)`. Iterates all `Server` objects, calls sync.py functions, writes per-server summary to job log.

**`__init__.py`** — `NetBoxKeaConfig.ready()` calls `_configure_sync_job_interval()` (patches in-memory registry from PLUGINS_CONFIG, no DB access) and `_heal_ghost_scheduled_jobs()` (removes ghost `scheduled`/`pending` DB records whose RQ counterpart is dead/missing — three-level exception nesting for startup safety).

**REST API** (`api/`) uses `NetBoxModelViewSet` + `NetBoxModelSerializer` — only the `Server` model is exposed. All password fields are write-only in the serializer.

**GraphQL** (`graphql.py`) uses strawberry-django: `@strawberry_django.type` for the model, `schema = [Query]`. NetBox picks this up automatically.

## Key Conventions

### Non-model tables

Lease, subnet, and reservation tables use `GenericTable(BaseTable)` instead of `NetBoxTable` because they have no Django model backing them. They accept plain `list[dict]`. `GenericTable` defines `objects_count` as a property returning `len(self.data)` to satisfy NetBox's pagination interface.

### HTMX pagination

Lease views serve two response types from the same `get()` method: a full page render and an HTMX partial (just the table fragment). The split is done with `htmx_partial(request, ...)` from `utilities.htmx`. Pagination state is passed via query params; the hidden `page` field uses `VeryHiddenInput` which renders as an empty string to avoid form conflicts.

### View registration

Standard CRUD views use `@register_model_view(Server)` / `@register_model_view(Server, "edit")` etc. Custom tabs (Status, Leases, Reservations, Subnets, Jobs) use `OptionalViewTab` from `utilities.py` — a subclass of NetBox's `ViewTab` that accepts `is_enabled: Callable[[Server], bool]` to conditionally hide tabs based on model state (e.g. hide DHCPv6 tab if `server.dhcp6` is False).

### Generic views with TypeVar

`BaseServerLeasesView` is `generic.ObjectView, Generic[T]` where `T = TypeVar("T", bound=BaseTable)`. Concrete subclasses (`ServerLeases6View`, `ServerLeases4View`) only need to declare `table_class`, `form_class`, `dhcp_version`, and `lease_service`. The base class handles pagination, HTMX, search, export, and delete routing.

### Fake model for GetReturnURLMixin

`BaseServerLeasesDeleteView` mixes in `GetReturnURLMixin` which expects `self.model`. Since leases aren't a real model, `FakeLeaseModel` / `FakeLeaseModelMeta` provide a minimal stand-in with just `app_label` and `model_name` so `get_return_url()` resolves correctly.

### URL structure

`get_model_urls("netbox_kea", "server")` from `utilities.urls` auto-generates standard NetBox object URLs (detail, edit, delete, changelog, journal). Custom routes (leases, reservations, subnets, shared networks, options, DHCP control) are declared explicitly before the `include()`.

### Forms

Lease search forms inherit from `BaseLeasesSarchForm` (note the typo — it's intentional/existing). Each subclass defines an inner `Meta` with `ip_version: Literal[4, 6]` which the base class `clean()` uses to validate subnet CIDR, IP addresses, and hardware addresses (via `is_hex_string()` from `utilities.py`).

### Protocol-aware `get_client()`

Always pass `version=` to `server.get_client()` when the DHCP version is known: `server.get_client(version=self.dhcp_version)`. Omitting it falls back to `ca_url` regardless of whether a protocol-specific URL is configured.

### API URL naming

The serializer's `HyperlinkedIdentityField` uses `view_name="plugins-api:netbox_kea-api:server-detail"` — the `plugins-api:` prefix and `-api:` namespace suffix are NetBox conventions.

## Kea API Reference

- **Primary**: `kea.readthedocs.io/en/latest/api.html` — 206 commands with full JSON schemas. `web_fetch` specific anchors when implementing a new command.
- **Live discovery**: run `list-commands` against the target server to confirm which hook libraries are loaded. Many commands require hooks.
- **Key hooks** and the commands they gate:
  - `host_cmds` — all `reservation-*` commands (open source since Kea 2.7.7 / MPL 2.0)
  - `lease_cmds` — `lease4/6-get-by-hostname/hw-address/state`, `lease4/6-update/add`
  - `subnet_cmds` — `subnet4/6-list/get/add/update` (alternative to `config-get`)
  - `stat_cmds` — `stat-lease4/6-get` for utilization per subnet
- **Hook detection pattern**: call `list-commands` with service, cache per request, show warning banner in UI if a required command is absent.
- **Pool operations**: support both Kea 2.x and 3.x APIs.

## Test Infrastructure

### Unit tests (`netbox_kea/tests/`)

No Docker required. All Kea HTTP calls are mocked. Key test files:

- `test_plugin_config.py` — `_heal_ghost_scheduled_jobs()`, `_configure_sync_job_interval()`, `ready()` wiring
- `test_jobs.py` — `KeaIpamSyncJob` sync logic, subnet/lease/reservation phases
- `test_sync.py` — `sync_lease_to_netbox()`, `sync_reservation_to_netbox()`, stale IP cleanup
- `test_sync_views.py` / `test_views_sync_jobs.py` — IPAM sync config UI and jobs tab views
- `test_views_leases.py`, `test_views_reservations.py`, `test_views_subnets.py`, etc. — feature views
- `test_kea_client.py` — `KeaClient` command dispatch, error handling
- `test_models.py` — `Server.clean()`, `SyncConfig.get()`, credential censoring

### Integration tests (`tests/`)

Require Docker. `tests/test_setup.sh` generates TLS certs, builds a wheel, and starts the compose stack.

`conftest.py` fixtures:
- `nb_api` (session-scoped, autouse) — pynetbox client; **deletes all existing Kea servers** before suite runs
- `netbox_token` — provisions a token via the API
- `nb_http` — `requests.Session` with auth headers pre-set
- `kea_basic_url` / `kea_basic_username` / `kea_basic_password` — nginx proxy with HTTP Basic auth
- `kea_https_url` — nginx with TLS

Test files:
- `tests/test_netbox_kea_api_server.py` — REST API CRUD via pynetbox
- `tests/test_ui.py` — Playwright browser tests covering the full UI
- `tests/kea.py` / `tests/constants.py` — shared test helpers
