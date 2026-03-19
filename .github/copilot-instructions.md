# Copilot Instructions — netbox-kea-ng

A NetBox plugin that integrates [Kea DHCP](https://www.isc.org/kea/) server management. Published to PyPI as **`netbox-kea-ng`** — a fork of [netbox-kea](https://github.com/devon-mar/netbox-kea) by Devon Mar. The Django app/module name remains `netbox_kea` (unchanged from upstream). Exposes a `Server` model representing a Kea Control Agent endpoint, with views for live daemon status, lease search/delete, and subnet listing.

## Build, Test & Lint

```bash
# Install dev dependencies
uv sync

# Lint
uv run ruff check netbox_kea/
uv run ruff format --check netbox_kea/

# REUSE compliance
uv run reuse lint

# Format
uv run ruff format netbox_kea/

# Build wheel (required before running tests)
uv build

# Run full test suite (requires Docker — spins up NetBox + Kea + nginx + postgres + redis)
./tests/test_setup.sh   # generates certs, builds Docker images, starts containers
uv run pytest --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml

# Run a single test file
uv run pytest tests/test_netbox_kea_api_server.py -v

# Run a single test function
uv run pytest tests/test_ui.py::test_function_name -v

# Install pre-commit hooks
uv run pre-commit install
```

CI tests against NetBox v4.0–v4.5 using a matrix build. Playwright traces on failure are uploaded as artifacts.

Ruff is configured in `pyproject.toml` — migrations are excluded from linting. Line length (E501) is ignored.

## Architecture

```text
URL request
  → urls.py             (routes to view classes)
  → views.py            (view calls server.get_client() → KeaClient)
  → kea.py              (HTTP POST to Kea Control Agent /api/v1/)
  → utilities.py        (format_leases, format_duration enrich raw Kea data)
  → tables.py           (non-model GenericTable renders enriched dicts)
  → template            (rendered with django-tables2 + HTMX for pagination)
```

**`Server` model** (`models.py`) is the only persisted model. It stores connection config and calls `get_client()` to produce a `KeaClient`. The `clean()` method actually connects to Kea to validate both DHCPv4 and DHCPv6 are reachable before saving.

**`KeaClient`** (`kea.py`) wraps a `requests.Session`. All API calls go through `.command(command, service, arguments)` which POSTs JSON to the Kea Control Agent. Responses are `list[KeaResponse]` — one entry per service. `check_response()` raises `KeaException` if any result code is not in `ok_codes`.

**Lease/subnet views** operate entirely on live Kea data — no Django ORM queries for leases. `format_leases()` in `utilities.py` enriches raw Kea lease dicts by computing `expires_at`, `expires_in`, and normalising hyphenated keys to underscores (so templates can access `record.valid_lft` etc.).

**REST API** (`api/`) uses `NetBoxModelViewSet` + `NetBoxModelSerializer` — only the `Server` model is exposed. Password is write-only in the serializer.

**GraphQL** (`graphql.py`) uses strawberry-django: define a `@strawberry_django.type` for the model and export `schema = [Query]`. NetBox picks this up automatically.

## Key Conventions

### Non-model tables

Lease and subnet tables use `GenericTable(BaseTable)` instead of `NetBoxTable` because they have no Django model backing them. They accept plain `list[dict]`. `GenericTable` defines `objects_count` as a property returning `len(self.data)` to satisfy NetBox's pagination interface.

### HTMX pagination

Lease views serve two response types from the same `get()` method: a full page render and an HTMX partial (just the table fragment). The split is done with `htmx_partial(request, ...)` from `utilities.htmx`. Pagination state is passed via query params; the hidden `page` field uses `VeryHiddenInput` which renders as an empty string to avoid form conflicts.

### View registration

Standard CRUD views use `@register_model_view(Server)` / `@register_model_view(Server, "edit")` etc. Custom tabs (Status, Leases, Subnets) use `OptionalViewTab` from `utilities.py` — a subclass of NetBox's `ViewTab` that accepts `is_enabled: Callable[[Server], bool]` to conditionally hide tabs based on model state (e.g. hide DHCPv6 tab if `server.dhcp6` is False).

### Generic views with TypeVar

`BaseServerLeasesView` is `generic.ObjectView, Generic[T]` where `T = TypeVar("T", bound=BaseTable)`. Concrete subclasses (`ServerLeases6View`, `ServerLeases4View`) only need to declare `table_class`, `form_class`, `dhcp_version`, and `lease_service`. The base class handles pagination, HTMX, search, export, and delete routing.

### Fake model for GetReturnURLMixin

`BaseServerLeasesDeleteView` mixes in `GetReturnURLMixin` which expects `self.model`. Since leases aren't a real model, `FakeLeaseModel` / `FakeLeaseModelMeta` provide a minimal stand-in with just `app_label` and `model_name` so `get_return_url()` resolves correctly.

### URL structure

`get_model_urls("netbox_kea", "server")` from `utilities.urls` auto-generates standard NetBox object URLs (detail, edit, delete, changelog, journal). Custom routes (leases, subnets) are declared explicitly before the `include()`.

### Forms

Lease search forms inherit from `BaseLeasesSarchForm` (note the typo — it's intentional/existing). Each subclass defines an inner `Meta` with `ip_version: Literal[4, 6]` which the base class `clean()` uses to validate subnet CIDR, IP addresses, and hardware addresses (via `is_hex_string()` from `utilities.py`).

### API URL naming

The serializer's `HyperlinkedIdentityField` uses `view_name="plugins-api:netbox_kea-api:server-detail"` — the `plugins-api:` prefix and `-api:` namespace suffix are NetBox conventions.

## Feature Roadmap & Development Approach

### Kea API Reference

- **Primary**: `kea.readthedocs.io/en/latest/api.html` — 206 commands with full JSON schemas. `web_fetch` specific anchors when implementing a new command.
- **Live discovery**: run `list-commands` against the target server (see `get_available_commands()` pattern) to confirm which hook libraries are loaded. Many commands require hooks (see below).
- **Key hooks** and the commands they gate:
  - `host_cmds` — all `reservation-*` commands (open source since Kea 2.7.7 / MPL 2.0)
  - `lease_cmds` — `lease4/6-get-by-hostname/hw-address/state`, `lease4/6-update/add`
  - `subnet_cmds` — `subnet4/6-list/get/add/update` (alternative to `config-get`)
  - `stat_cmds` — `stat-lease4/6-get` for utilization per subnet
- **Hook detection pattern**: call `list-commands` with service, cache per request, show warning banner in UI if a required command is absent.

### Phase Plan (Priority Order)

#### Phase 1 — Dual-URL Server (model migration) [P1]

Add optional `dhcp4_url` / `dhcp6_url` fields to `Server` so a single object can point to separate
`kea-v4-api.cnad.dev` and `kea-v6-api.cnad.dev` processes. Also add `has_control_agent` boolean.
- `get_client()` gains a `version: Literal[4, 6] | None` parameter — returns a `KeaClient` at the
  correct URL for that protocol.
- Status view: `_get_ca_status()` is only called when `has_control_agent=True`; direct-daemon mode
  shows per-service status without the misleading "Control Agent" row.
- DB migration required. Backward compatible: existing configs (single `server_url`) keep working.

#### Phase 2 — Reservation Management [P2]

Full CRUD against `host_cmds` hook. Requires Phase 1 (protocol-aware client).
- `ServerReservations4/6View` list tabs with `reservation-get-page` pagination
- `ServerReservationAdd/Edit/DeleteView` using `reservation-add/update/del`
- v4 identifier: `hw-address`; v6 identifier: `duid`. Optional `hostname`, `client-classes`, `option-data`.
- Degrade gracefully if `host_cmds` not loaded.

#### Phase 3 — NetBox IPAM Integration [P3]

Replace the brittle external IP-update script.
- **3a**: "Sync to NetBox IP" bulk action on lease table → create/update `IPAddress` (status=active,
  dns_name from hostname, optional interface assignment by MAC).
- **3b**: Reservation form links to a NetBox `IPAddress`; saving creates/updates IP with status=reserved.
- **3c**: Action on NetBox `IPAddress` detail → "Create Kea Reservation" (prefilled).

#### Phase 4 — DNS via netbox-dns IPAMDNSsync [P4]

No direct netbox-dns API calls needed. Set `dns_name` on `IPAddress` (from lease hostname / reservation) →
netbox-dns IPAMDNSsync auto-creates A/AAAA/PTR records via Django signals, provided DNS views + zones exist.
- Check `importlib.util.find_spec("netbox_dns")` at runtime; optional `NETBOX_KEA_DNS_SYNC` plugin setting.
- No hard dependency.

#### Phase 5 — Subnet Utilization Statistics [P5]

- `stat-lease4/6-get` (requires `stat_cmds`) → add utilization % column to subnet tables.
- Degrade gracefully.

### Convention: protocol-aware `get_client()`

After Phase 1, all view code that currently calls `server.get_client()` should pass the protocol
version where known: `server.get_client(version=self.dhcp_version)`. This is the primary breaking
change to be aware of when updating views.

## Test Infrastructure

Tests require Docker. `tests/test_setup.sh` generates TLS certs, builds a wheel, and starts the compose stack. The compose stack includes: NetBox, netbox-worker, postgres, redis, nginx (with basic auth + TLS), kea-ctrl-agent, kea-dhcp4, kea-dhcp6.

`conftest.py` fixtures:
- `nb_api` (session-scoped, autouse) — creates a pynetbox API client and **deletes all existing Kea servers** before the suite runs
- `netbox_token` — provisions a token via the API (handles both v1 `key` and v2 `nbt_` format)
- `nb_http` — a `requests.Session` with auth headers pre-set
- `kea_basic_url` / `kea_basic_username` / `kea_basic_password` — point to the nginx proxy with HTTP Basic auth
- `kea_https_url` — nginx with TLS

Test files:
- `tests/test_netbox_kea_api_server.py` — REST API CRUD tests using pynetbox
- `tests/test_ui.py` — Playwright browser tests covering the full UI
- `tests/kea.py` / `tests/constants.py` — shared test helpers
