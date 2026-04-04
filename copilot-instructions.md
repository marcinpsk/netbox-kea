# Copilot Instructions — netbox-kea-ng

A NetBox plugin that integrates [Kea DHCP](https://www.isc.org/kea/) server management. Published to PyPI as **`netbox-kea-ng`** — a fork of [netbox-kea](https://github.com/devon-mar/netbox-kea) by Devon Mar. The Django app/module name remains `netbox_kea` (unchanged from upstream). Exposes a `Server` model representing a Kea Control Agent endpoint, with views for live daemon status, lease search/delete, reservation CRUD, subnet/pool/shared-network management, and NetBox IPAM sync.

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

# Build wheel (required before running integration tests)
uv build

# Run unit tests (no Docker required; mocks Kea HTTP calls)
uv run pytest                                              # all unit tests
uv run pytest netbox_kea/tests/test_views_leases.py -v     # single file
uv run pytest netbox_kea/tests/test_views_leases.py::TestClass::test_method -v  # single test

# Run integration tests (requires Docker — spins up NetBox + Kea + nginx + postgres + redis)
./tests/test_setup.sh   # generates certs, builds Docker images, starts containers
uv run pytest tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml

# Install pre-commit hooks
uv run pre-commit install
```

Unit tests live in `netbox_kea/tests/`. `pythonpath` is set to `/opt/netbox/netbox` and `DJANGO_SETTINGS_MODULE=netbox.settings`.

Integration tests live in `tests/`. E2E tests live in `e2e/`. CI tests against NetBox v4.0–v4.5 using a matrix build. Playwright traces on failure are uploaded as artifacts.

Ruff is configured in `pyproject.toml` — migrations are excluded from linting. Line length 120, max complexity 15. E501 ignored (handled by formatter). Docstrings required except in tests/migrations/`__init__.py`.

## Architecture

```text
URL request
  → urls.py             (routes to view classes)
  → views/              (view modules call server.get_client() → KeaClient)
  → kea.py              (HTTP POST to Kea Control Agent /api/v1/)
  → utilities.py        (format_leases, format_duration enrich raw Kea data)
  → sync.py             (bridges Kea data to NetBox IPAM)
  → tables.py           (non-model GenericTable renders enriched dicts)
  → template            (rendered with django-tables2 + HTMX for pagination)
```

### Views directory structure

Views are split into focused modules under `netbox_kea/views/`:

| Module | Responsibility |
|--------|---------------|
| `_base.py` | `ConditionalLoginRequiredMixin`, `_KeaChangeMixin`, shared helpers |
| `server.py` | Server CRUD (list, detail, edit, delete, bulk operations) |
| `combined.py` | Combined status badge, server status tab |
| `leases.py` | Lease search/delete/add/edit, badge enrichment, MAC-based pending-IP detection |
| `reservations.py` | Reservation CRUD (add/edit/delete), lease-status enrichment |
| `subnets.py` | Subnet/pool CRUD, subnet wipe |
| `shared_networks.py` | Shared network CRUD (add/edit/delete) |
| `options.py` | DHCP option views (subnet/network option editing) |
| `sync_views.py` | Sync views (lease→NetBox, reservation→NetBox, bulk sync) |
| `dhcp_control.py` | DHCP service enable/disable control |

### Core components

- **`Server` model** (`models.py`): Only persisted model. Stores connection config including optional `dhcp4_url`/`dhcp6_url` for dual-URL mode. `get_client(version=4|6|None)` returns a protocol-aware `KeaClient`. The `clean()` method performs live connectivity checks before saving.
- **`KeaClient`** (`kea.py`): Wraps `requests.Session`. All API calls go through `.command(command, service, arguments)` which POSTs JSON to the Kea Control Agent. Responses are `list[KeaResponse]`. `check_response()` raises `KeaException` if any result code is not in `ok_codes`. Supports `.clone()` for thread-safe concurrent lookups.
- **`sync.py`**: Bridges Kea data to NetBox IPAM — `sync_lease_to_netbox()` (status=dhcp/active), `sync_reservation_to_netbox()` (status=reserved/active). Handles stale IP cleanup with `cleanup_stale_ips_batch()` grouped by `(hostname, address_family)`.
- **REST API** (`api/`): `NetBoxModelViewSet` + `NetBoxModelSerializer` — only the `Server` model is exposed. Password is write-only in the serializer.
- **GraphQL** (`graphql.py`): strawberry-django types, auto-discovered by NetBox.

### Exception hierarchy

```text
Exception
 └── KeaException                  # Base — any non-ok result from Kea
      ├── KeaConfigTestError       # config-test failed
      ├── KeaConfigPersistError    # config-set / config-write failed
      │    └── PartialPersistError # config-set succeeded but config-write failed
      │         └── AmbiguousConfigSetError  # config-set status is ambiguous
      └── (generic Kea errors)
```

**Catch order matters**: always catch `PartialPersistError` *before* `KeaException`. `AmbiguousConfigSetError` → `PartialPersistError` → `KeaException`.

## Security & Code Quality Rules

- **Never leak exception details to HTTP responses.** Use `logger.exception()` for server-side logging and return a generic message like `"An internal error occurred"` to users. Raw `str(exc)` can expose internal URLs, TLS details, or Kea API config.
- **Always pass `version=` to `server.get_client()`** when the DHCP version is known (e.g., `server.get_client(version=self.dhcp_version)`). Omitting it may hit the wrong endpoint when dual URLs are configured.
- **Always call `server.get_client()` inside a try block** — client creation can fail with `ValueError` or `requests.RequestException` due to bad config or connectivity. Never call it at module/class level or before error handling is in scope.
- **Validate Kea response shape before indexing.** After `client.command()`, check `resp` is a non-empty list and `resp[0]` is a dict before accessing `resp[0]["arguments"]`. Check `arguments` is a dict and nested keys (e.g., `"leases"`, `"subnet4"`) are lists before indexing into them. Malformed payloads should raise `RuntimeError` to hit existing error handlers.
- **Catch `(KeaException, requests.RequestException, ValueError)` consistently** in mutation handlers (add/edit/delete). Split `KeaException` from the others when you need `kea_error_hint(exc)` for hook-related errors (result=2). Always catch `PartialPersistError` before `KeaException`.
- **Use `kea_error_hint(exc)` for user-facing Kea error messages.** It maps result codes to actionable hints (result=2 → hook library not loaded, result=128 → daemon unreachable).
- **Guard action URLs/buttons by permission AND lookup state.** Don't offer Sync/Reserve buttons for leases where the reservation lookup failed (check `failed_ips` and `failed_mac_keys`). Don't offer add/edit URLs to users without `change` permission.
- **Django form querysets must be evaluated at instantiation, not class definition time.** Use `__init__` to set `self.fields["field"].queryset` dynamically — class-level querysets become stale in long-running processes.
- **django-tables2 Column instances must not be shared across table classes.** Use a factory function instead of a module-level instance to avoid shared-state bugs.
- **Catch `DatabaseError` (not just `ProgrammingError`/`OperationalError`)** around `JournalEntry.objects.create` and similar DB writes in non-critical paths. This prevents a successful Kea operation from turning into a 500 due to a DB schema mismatch.
- **DHCPv6 reservations use `ip-addresses` (list), not `ip-address` (string).** Always check both fields when inspecting reservation data.

## Key Patterns

### Non-model tables

Lease, subnet, reservation, and shared-network tables use `GenericTable(BaseTable)` instead of `NetBoxTable` because they have no Django model backing them. They accept plain `list[dict]`. `GenericTable` defines `objects_count` as a property returning `len(self.data)` to satisfy NetBox's pagination interface.

### HTMX pagination

Lease views serve two response types from the same `get()` method: a full page render and an HTMX partial (just the table fragment). The split is done with `htmx_partial(request, ...)` from `utilities.htmx`. Pagination state is passed via query params; the hidden `page` field uses `VeryHiddenInput` which renders as an empty string to avoid form conflicts.

### View registration

Standard CRUD views use `@register_model_view(Server)` / `@register_model_view(Server, "edit")` etc. Custom tabs (Status, Leases, Subnets, Reservations, Shared Networks) use `OptionalViewTab` from `utilities.py` — a subclass of NetBox's `ViewTab` that accepts `is_enabled: Callable[[Server], bool]` to conditionally hide tabs based on model state (e.g. hide DHCPv6 tab if `server.dhcp6` is False).

### Generic views with TypeVar

`BaseServerLeasesView` is `generic.ObjectView, Generic[T]` where `T = TypeVar("T", bound=BaseTable)`. Concrete subclasses (`ServerLeases6View`, `ServerLeases4View`) only need to declare `table_class`, `form_class`, `dhcp_version`, and `lease_service`. The base class handles pagination, HTMX, search, export, and delete routing.

### Fake model for GetReturnURLMixin

`BaseServerLeasesDeleteView` mixes in `GetReturnURLMixin` which expects `self.model`. Since leases aren't a real model, `FakeLeaseModel` / `FakeLeaseModelMeta` provide a minimal stand-in with just `app_label` and `model_name` so `get_return_url()` resolves correctly.

### Lease badge enrichment pipeline

`_enrich_leases_with_badges()` in `leases.py` orchestrates a two-phase reservation lookup:
1. **Phase 1a — IP-based**: `_fetch_reservation_by_ip_for_leases()` uses `ThreadPoolExecutor` with `client.clone()` workers to look up reservations by IP in parallel.
2. **Phase 1b — MAC-based**: `_fetch_reservation_by_mac_for_leases()` looks up reservations by `hw-address` for leases that had no IP match, detecting pending IP changes (device has reservation at a different IP). Returns `(reservation_by_mac, failed_keys)` tuple.

Both phases use composite `(mac, subnet_id)` keys for deduplication and failure tracking. The `_FETCH_ERROR` sentinel distinguishes lookup errors from genuine not-found (`None`). Failed keys are tracked in `failed_mac_keys: set[tuple[str, int]]` and propagated to prevent offering actions on leases with indeterminate state.

### KeaClient.clone() for concurrent lookups

`client.clone()` creates a new `KeaClient` sharing the same config but with a fresh `requests.Session` — necessary because `requests.Session` is not thread-safe. Always use `with client.clone() as worker_client:` inside thread pool workers.

### Sync lifecycle

IP status follows a semantic lifecycle:
- `dhcp` — dynamic lease, no reservation (ephemeral)
- `reserved` — reservation only, no active lease (admin intent)
- `active` — both reservation AND active lease (planned + in use)

`cleanup_stale_ips_batch()` groups by `(hostname, address_family)` to avoid v4 cleanup deleting v6 entries. Batch cleanup is skipped when errors > 0 to avoid data loss. Single-sync paths (`_sync()`) intentionally use `cleanup=False`; a one-record sync never has a complete keep-set so cleanup must be deferred to the batch path.

### Kea DHCP option aliases

DNS options can be either `domain-name-servers` or `dns-servers`; NTP can be `ntp-servers` or `sntp-servers`. When searching for existing option metadata (e.g., `always-send`), search both alias tuples to avoid losing settings.

### URL structure

`get_model_urls("netbox_kea", "server")` from `utilities.urls` auto-generates standard NetBox object URLs (detail, edit, delete, changelog, journal). Custom routes (leases, subnets, reservations, shared-networks) are declared explicitly before the `include()`.

### Forms

Lease search forms inherit from `BaseLeasesSarchForm` (note the typo — it's intentional/existing). Each subclass defines an inner `Meta` with `ip_version: Literal[4, 6]` which the base class `clean()` uses to validate subnet CIDR, IP addresses, and hardware addresses (via `is_hex_string()` from `utilities.py`).

CSV form fields (like `dns_servers`, `ntp_servers`) must have `clean_<field>` methods that split on commas, strip whitespace, drop empty entries, and rejoin — otherwise stray whitespace passes through and fails Kea validation.

### API URL naming

The serializer's `HyperlinkedIdentityField` uses `view_name="plugins-api:netbox_kea-api:server-detail"` — the `plugins-api:` prefix and `-api:` namespace suffix are NetBox conventions.

## Testing Patterns

### Unit test infrastructure

Unit tests in `netbox_kea/tests/` mock all Kea HTTP calls. Key patterns:

- **Mock path**: When patching `KeaClient`, patch `netbox_kea.models.KeaClient` (not the view module) — `server.get_client()` instantiates from `models.py`.
- **Auth**: Use `api_client.force_authenticate(user=self.user)` not token credentials — NetBox v4 tokens use `nbt_` format.
- **User model**: Always use `get_user_model()` instead of `from django.contrib.auth.models import User` — NetBox swaps the User model.
- **DB-less model tests**: Use `SimpleTestCase` + `patch.object(NetBoxModel, 'clean')` for model validation without DB. Use `@override_settings(PLUGINS_CONFIG=...)` when calling `get_client()`.
- **BulkImportView POST**: Requires `data=`, `format='csv'`, `csv_delimiter=','`. Missing `csv_delimiter` causes validation error.
- **Django Debug Toolbar**: Blocks Playwright pointer events. Use `page.goto()` for navigation; for forms use `page.evaluate('form.submit()')` with `page.expect_navigation()`.

### Test file mapping

| Test file | Tests for |
|-----------|-----------|
| `test_views_leases.py` | Lease views, badge enrichment, MAC-based detection |
| `test_views_reservations.py` | Reservation CRUD views |
| `test_views_subnets.py` | Subnet/pool views |
| `test_views_shared_networks.py` | Shared network CRUD |
| `test_views_options.py` | DHCP option editing |
| `test_views_sync.py` | Sync views (lease/reservation → NetBox) |
| `test_views_combined.py` | Combined status badge |
| `test_sync.py` | sync.py logic (stale cleanup, IP lifecycle) |
| `test_kea_client.py` | KeaClient, response parsing, clone() |
| `test_models.py` | Server model validation, get_client() |
| `test_forms.py` | Form validation |
| `test_api_leases.py` | Lease API endpoints |
| `test_api_reservations.py` | Reservation API endpoints |

### Integration & E2E tests

Integration tests in `tests/` require Docker. The compose stack includes: NetBox, netbox-worker, postgres, redis, nginx (basic auth + TLS), kea-ctrl-agent, kea-dhcp4, kea-dhcp6.

`conftest.py` fixtures:
- `nb_api` (session-scoped, autouse) — creates a pynetbox API client and deletes all existing Kea servers before the suite runs
- `netbox_token` — provisions a token via the API (handles both v1 `key` and v2 `nbt_` format)
- `nb_http` — a `requests.Session` with auth headers pre-set
- `kea_basic_url` / `kea_basic_username` / `kea_basic_password` — point to the nginx proxy with HTTP Basic auth
- `kea_https_url` — nginx with TLS

E2E tests live in `e2e/` and are separate from both unit and integration tests.

## Conventions

- **Commit messages**: Conventional Commits (feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert). Enforced by pre-commit hook.
- **Ruff config**: Line length 120, max complexity 15. Migrations excluded. E501 ignored (handled by formatter). Docstrings required except in tests/migrations/`__init__.py`.
- **URL patterns**: `get_model_urls("netbox_kea", "server")` auto-generates standard CRUD routes. Custom routes declared explicitly before the `include()`.
- **API URL naming**: `view_name="plugins-api:netbox_kea-api:server-detail"` — `plugins-api:` prefix and `-api:` namespace are NetBox conventions.

## Kea API Reference

- **Primary**: `kea.readthedocs.io/en/latest/api.html` — 206 commands with full JSON schemas.
- **Live discovery**: run `list-commands` against the target server to confirm which hook libraries are loaded.
- **Key hooks** and the commands they gate:
  - `host_cmds` — all `reservation-*` commands (open source since Kea 2.7.7 / MPL 2.0)
  - `lease_cmds` — `lease4/6-get-by-hostname/hw-address/state`, `lease4/6-update/add`
  - `subnet_cmds` — `subnet4/6-list/get/add/update`
  - `stat_cmds` — `stat-lease4/6-get` for utilization per subnet
- **Hook detection pattern**: call `list-commands` with service, cache per request, show warning banner in UI if a required command is absent. Only set `hook_available=False` on result code 2 (command not supported).
