<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# opengrep ruleset

Custom [opengrep](https://github.com/opengrep/opengrep) rules that encode this
project's `AGENTS.md` security/correctness invariants as machine-checked gates,
so the same classes of bug stop coming back review after review.

## Why opengrep (and not just ruff / CodeQL)

- **ruff** (incl. the `S` / flake8-bandit rules) covers generic Python lint and
  security smells, but can't express project-specific call-shape rules.
- **CodeQL** (`.github/workflows/codeql.yml`) covers broad dataflow SAST.
- **opengrep** fills the gap: cheap, readable YAML patterns for *our* invariants
  — and it is the same engine **CodeRabbit** runs.

## Relationship to CodeRabbit

CodeRabbit skips its OpenGrep analysis when it detects OpenGrep in CI.
The custom rules run locally through the **pre-commit hook**. Keep OpenGrep
out of CI so CodeRabbit can run its own analysis on pull requests.

## Layout

| Path | Purpose |
| --- | --- |
| `.opengrep/kea-rules.yaml` | The ruleset. **Single source of truth.** Used by the pre-commit hook. |
| `.opengrep/tests/*.py` | Annotated rule-test fixtures (`# ruleid:` must match, `# ok:` must not). |
| `.semgrepignore` | Replaces opengrep's built-in ignore list, which skips every `tests/` directory. A rule that must not apply to tests excludes `**/tests/**` itself. |
| `scripts/opengrep-scan.sh` | Scan the source tree; used by the pre-commit hook. Exits non-zero on any finding. |
| `scripts/opengrep-test.sh` | Run the rule-tests against the ruleset. |

## Rules

| Rule id | Severity | Catches |
| --- | --- | --- |
| `kea-queryset-model-attribute-discriminator` | error | A `model` attribute check used to distinguish querysets from model instances. Model fields can use the same name. |
| `kea-config-phase-without-reply-validation` | error | Direct config-test, config-set, or config-write calls that bypass `_one_command()` or `_config_mutation_command()` and their single-reply validation. |
| `kea-sync-hostname-unvalidated` | error | Raw hostname reads in lease reconciliation and claims that bypass the shared string-or-null validator. |
| `kea-get-client-missing-version` | warning | `server.get_client()` without `version=` (wrong daemon on dual-URL servers). |
| `kea-exception-detail-in-response` | error | `str(exc)` / f-string of a caught exception leaked into `messages.*` / HTTP / DRF responses. |
| `kea-command-result-indexed-without-guard` | error | `client.command(...)[0]` indexed directly, before validating the response shape. |
| `kea-reservation-command-outside-adapter` | error | A `reservation-*` Kea command sent outside `kea.py`, which would put raw Reservation records in front of a consumer that cannot read their keys. |
| `kea-config-read-modify-write-without-reader` | error | A `config-get` read in a function that calls `config_set()`, bypassing `config_candidate()` and its `Dhcp4`/`Dhcp6` shape check. |
| `kea-unsupported-command-inline-check` | error | `exc.response.get("result") == 2` (or `["result"]`) outside `KeaException`; use the `unsupported_command` property. |
| `kea-config-change-rejection-caught-outside-mapper` | error | An `except` that names `ConfigChangeRejected` in `views/` outside a function named `_run_config_change()`. The one such function, in `views/_base.py`, is the one place that turns a Configuration Change outcome or rejection into a message. |
| `kea-config-change-mapper-outside-base` | error | A `def _run_config_change` under `views/` in any file but `views/_base.py`. It closes the name exemption of the rule above. |
| `kea-config-change-in-broad-except` | error | A `config_write` call under `views/` inside a `try` whose `except` catches `Exception`, `BaseException`, or everything. That handler would also catch `ConfigChangeRejected`. A call passed to `_run_config_change()` is exempt, because the mapper catches the rejection first. |
| `kea-http-call-outside-client` | error | `requests.Session()`, `requests.get()`, `post()`, `put()`, `patch()`, `delete()`, `head()` or `request()` in `netbox_kea/` outside `kea.py` and the tests. `KeaClient.command()` is the one seam that applies the Server TLS settings on each request and reports a missing TLS file as a `RequestException`. Limit: it matches the `requests.` module prefix only, so an aliased import (`from requests import post`, `import requests as r`) is not detected. |
| `kea-mac-sync-write-without-savepoint` | error | Direct MAC lookup and save calls in `sync_mac_address()` outside `event_scope.atomic()`. The guard covers this synchronizer's savepoint boundary, not transaction ownership across other callers. |
| `kea-no-event-queue-internals` | error | `events_queue`, `EventContext` or `enqueue_event` in `netbox_kea/` outside the tests. NetBox owns its event queue; `event_scope.atomic()` uses only `event_tracking`. Limit: it matches the names as text, so a comment that names them also matches. |
| `kea-raw-atomic` | error | `django.db.transaction.atomic` (call, `with`, decorator, aliased import) in `netbox_kea/` outside `event_scope.py` and the tests. Every plugin transaction goes through `event_scope.atomic()`, which decides whether the block is a unit. |
| `kea-refusal-after-event-write` | error | A `raise` after `save()`, `delete()`, `create()`, `get_or_create()`, `update_or_create()` or an m2m `set()`, `add()`, `remove()`, `clear()` in an `event_scope.atomic()` block inside a `try`. Inside a transaction the block is a savepoint, and NetBox dispatches the event of the write that it rolled back. Limit: writes and raises inside called helpers and callbacks are not seen. |
| `kea-dispatch-error-swallowed` | error | An `event_scope.atomic()` block in a `try` whose `except Exception`, `except BaseException` or bare `except` has no `except event_scope.EventDispatchError: raise` in the same `try`. Limits: the handler order is not checked; a tuple that names `Exception`, a `try` with `finally`, and handlers in callers are not seen. The one reviewed broad handler above units is the IPAM job's per-Server catch in `jobs.py`, which logs the error and counts it in the job report. |
| `subprocess-without-timeout` | error | `subprocess.run()`, `call()`, `check_call()`, or `check_output()` without `timeout=`, in the package, the scripts, or the tests. A child process that stops responding would block the caller with no limit. |
| `netbox-ipam-get-or-create-non-unique-key` | error | `get_or_create()` or `update_or_create()` on `Prefix`, `IPRange`, or `IPAddress`, directly or on a queryset chain such as `.filter(...)`. An exact `pk=` or `id=` lookup is exempt. NetBox does not enforce these keys in the database, so a duplicate row makes every call raise `MultipleObjectsReturned`. |
| `netbox-ipam-mutation-outside-reconciliation` | error | Deletes and status writes on `IPAddress`, `Prefix` and `IPRange` outside `netbox_kea/ipam_reconciliation.py`. Covers direct queryset chains, local query bindings and typed helper parameters, including import aliases. Tests and migrations are excluded. |
| `kea-exception-handler-misses-runtime-error` | error | A `try` in `views/`, `api/`, `templatetags/`, `forms.py` or `server_connection.py` with an `except` that names `KeaException` and no sibling `except` for `RuntimeError`, `Exception`, `BaseException`, or everything. `check_response()` raises `RuntimeError` for a malformed reply entry, so the request fails with HTTP 500. Limits: a `try` with a `finally` or `else` clause, and a `RuntimeError` caught by an outer `try`, are not seen. |
| `kea-request-number-without-ascii-check` | error | `str.isdigit()`, `isdecimal()` or `isnumeric()`, a `\d` in an `re` pattern, and `int()` of a request value: `request.GET`, `POST`, `query_params` or `data`, a `params` mapping, or form `cleaned_data`, directly or through a local variable. These also accept other Unicode digits, so `?subnet_id=١٢` reads Subnet 12. Use `parse_decimal()` from `netbox_kea/decimal_text.py`. Limit: a value passed to another function as a parameter is not followed. |

The adjacent snapshot rule uses the stdlib AST checker
`netbox_kea/tests/snapshot_discipline.py`. It checks that loaded model saves take
a snapshot before their first direct mutation or known mutation-helper call.
It also rejects a later snapshot that overwrites the old values. An explicit
inventory classifies direct save sites; new or changed sites require review.
The checker runs in the native suite and the `snapshot-discipline` pre-commit
hook. Its direct-receiver scope and limits are documented in
[the design record](../docs/design/snapshot-discipline.md).
It has no baseline or inline suppression marker.

## Running locally

```bash
# Scan (same as the pre-commit hook):
./scripts/opengrep-scan.sh

# Run the rule-tests:
./scripts/opengrep-test.sh
```

Both scripts find opengrep via `$OPENGREP_BIN`, then `PATH`, then
`~/.local/opt/opengrep/bin`. Install opengrep from
<https://github.com/opengrep/opengrep> (or set `OPENGREP_BIN`).

## How it gates commits

The `opengrep` hook in `.pre-commit-config.yaml` runs at the **pre-commit** stage
when Python files, `.opengrep/`, OpenGrep scripts, or the hook configuration change.
It scans the whole package, `scripts/`, and `tests/`. Install the hook types once:

```bash
uv run --native-tls pre-commit install --install-hooks
```

The custom OpenGrep gate runs locally only. A commit made with `--no-verify`
bypasses it.

## Suppressing a true exception

Add an inline `# nosemgrep: <rule-id>` on the offending line, with a short reason
comment above it — see `views/server.py` (the version-agnostic Control Agent
status call).

## Adding a rule

1. Add the rule to `.opengrep/kea-rules.yaml`.
2. Add a fixture `.opengrep/tests/<rule-id>.py` with `# ruleid:` / `# ok:` lines.
3. `./scripts/opengrep-test.sh` — confirm it passes.
4. `./scripts/opengrep-scan.sh` — confirm the existing tree is clean (or fix it).
