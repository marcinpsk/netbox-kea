# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NetBox plugin for Kea DHCP server management. Published to PyPI as **`netbox-kea-ng`** (fork of netbox-kea by Devon Mar). The Django app/module name is `netbox_kea`. Exposes a `Server` model representing a Kea Control Agent endpoint, with views for live daemon status, lease search/delete, reservation CRUD, subnet/pool/shared-network management, and NetBox IPAM sync.

## Build, Test & Lint

```bash
uv sync                                    # install dev dependencies
uv build                                   # build wheel (required before integration tests)
uv run ruff check netbox_kea/              # lint
uv run ruff format --check netbox_kea/     # check formatting
uv run ruff format netbox_kea/             # auto-format
uv run reuse lint                          # SPDX/REUSE compliance
uv run pre-commit install                  # install pre-commit hooks
```

### Unit tests (no Docker required)

Default `testpaths` in pyproject.toml is `netbox_kea/tests`. These tests mock Kea HTTP calls.

```bash
uv run pytest                                              # run all unit tests
uv run pytest netbox_kea/tests/test_views_leases.py -v     # single file
uv run pytest netbox_kea/tests/test_views_leases.py::TestClass::test_method -v  # single test
```

Note: `pythonpath` is set to `/opt/netbox/netbox` and `DJANGO_SETTINGS_MODULE=netbox.settings` — unit tests expect a NetBox installation at that path.

### Integration tests (Docker required)

```bash
./tests/test_setup.sh                      # generates certs, builds images, starts compose stack
uv run pytest tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml
```

The compose stack runs: NetBox, netbox-worker, postgres, redis, nginx (basic auth + TLS), kea-ctrl-agent, kea-dhcp4, kea-dhcp6. Integration tests include REST API tests (pynetbox) and Playwright browser tests.

E2E tests live in `e2e/` and are separate from both unit and integration tests.

### CI

GitHub Actions matrix tests against NetBox v4.0–v4.5. Playwright traces uploaded as artifacts on failure.

## Architecture

```
URL request
  → urls.py             (routes to view classes)
  → views/              (view modules call server.get_client() → KeaClient)
  → kea.py              (HTTP POST to Kea Control Agent /api/v1/)
  → utilities.py        (format_leases, format_duration enrich raw Kea data)
  → sync.py             (bridges Kea data to NetBox IPAM)
  → tables.py           (non-model GenericTable renders enriched dicts)
  → template            (rendered with django-tables2 + HTMX for pagination)
```

### Core components

- **`Server` model** (`models.py`): Only persisted model. Stores Kea connection config. `get_client(version=4|6|None)` returns a protocol-aware `KeaClient`. `clean()` performs live connectivity checks before saving.
- **`KeaClient`** (`kea.py`): Wraps `requests.Session`. All Kea API calls go through `.command(command, service, arguments)` which POSTs JSON to `/api/v1/`. Methods for leases, reservations, subnets, pools, status, config. `.clone()` creates a thread-safe copy for concurrent lookups.
- **`sync.py`**: Bridges Kea data to NetBox IPAM — `sync_lease_to_netbox()` (status=dhcp/active), `sync_reservation_to_netbox()` (status=reserved/active). Handles stale IP cleanup with `cleanup_stale_ips_batch()`.
- **REST API** (`api/`): `NetBoxModelViewSet` for `Server` only. Password is write-only.
- **GraphQL** (`graphql.py`): strawberry-django types, auto-discovered by NetBox.

### Key patterns

- **Non-model tables**: Lease/subnet/reservation tables use `GenericTable(BaseTable)` — accept `list[dict]`, define `objects_count` as `len(self.data)` for pagination.
- **HTMX pagination**: Lease views serve full page or HTMX partial from same `get()` via `htmx_partial()`. Hidden `page` field uses `VeryHiddenInput` (renders empty string).
- **Generic views with TypeVar**: `BaseServerLeasesView` is `Generic[T]` where T is bound to `BaseTable`. Subclasses declare `table_class`, `form_class`, `dhcp_version`, `lease_service`.
- **FakeLeaseModel**: Minimal stand-in with `app_label`/`model_name` so delete views can use `GetReturnURLMixin`.
- **OptionalViewTab**: Subclass of `ViewTab` accepting `is_enabled: Callable[[Server], bool]` to conditionally hide tabs (e.g., hide DHCPv6 tab when `server.dhcp6` is False).
- **Protocol-aware client**: Views call `server.get_client(version=self.dhcp_version)`. Dual-URL support via `dhcp4_url`/`dhcp6_url` fields.
- **Lease search forms**: Inherit from `BaseLeasesSarchForm` (intentional existing typo). Inner `Meta.ip_version` drives validation.

### Kea API integration

- Hook detection: call `list-commands` with service, cache per request, show warning if required command absent.
- Key hooks: `host_cmds` (reservations), `lease_cmds` (lease queries), `subnet_cmds` (subnet management), `stat_cmds` (utilization stats).
- Pool operations support both Kea 2.x and 3.x APIs.

## Security & Code Quality Rules

- **Never leak exception details to HTTP responses.** Use `logger.exception()` for server-side logging and return a generic message like `"An internal error occurred"` to users. Raw `str(exc)` can expose internal URLs, TLS details, or Kea API config.
- **Always pass `version=` to `server.get_client()`** when the DHCP version is known (e.g., `server.get_client(version=self.dhcp_version)`). Omitting it may hit the wrong endpoint when dual URLs are configured.
- **Always call `server.get_client()` inside a try block** — client creation can fail with `ValueError` (e.g., invalid URL or missing protocol config). Never call it before error handling is in scope.
- **Validate Kea response shape before indexing.** After `client.command()`, check `resp` is a non-empty list and `resp[0]` is a dict before accessing `resp[0]["arguments"]`. Malformed payloads should raise `RuntimeError`.
- **Catch `(KeaException, requests.RequestException, ValueError)` consistently** in mutation handlers. Split `KeaException` when using `kea_error_hint(exc)`. Always catch `PartialPersistError` before `KeaException`.
- **Guard action URLs/buttons by permission AND lookup state.** Don't offer Sync/Reserve for leases where reservation lookup failed (check `failed_ips` and `failed_mac_keys`).
- **Django form querysets must be evaluated at instantiation, not class definition time.** Use `__init__` to set `self.fields["field"].queryset` dynamically — class-level querysets become stale in long-running processes.
- **django-tables2 Column instances must not be shared across table classes.** Use a factory function instead of a module-level instance to avoid shared-state bugs.
- **DHCPv6 reservations use `ip-addresses` (list), not `ip-address` (string).** Always check both fields when inspecting reservation data.
- **Catch `DatabaseError` around non-critical DB writes** (e.g., `JournalEntry.objects.create`) to prevent a successful Kea operation from turning into a 500.

## Conventions

- **Commit messages**: Conventional Commits (feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert). Enforced by pre-commit hook.
- **Ruff config**: Line length 120, max complexity 15. Migrations excluded. E501 ignored (handled by formatter). Docstrings required except in tests/migrations/`__init__.py`.
- **URL patterns**: `get_model_urls("netbox_kea", "server")` auto-generates standard CRUD routes. Custom routes (leases, subnets, reservations) declared explicitly before the `include()`.
- **API URL naming**: `view_name="plugins-api:netbox_kea-api:server-detail"` — `plugins-api:` prefix and `-api:` namespace are NetBox conventions.
