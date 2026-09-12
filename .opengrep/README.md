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
| `scripts/opengrep-scan.sh` | Scan the source tree; used by the pre-commit hook. Exits non-zero on any finding. |
| `scripts/opengrep-test.sh` | Run the rule-tests against the ruleset. |

## Rules

| Rule id | Severity | Catches |
| --- | --- | --- |
| `kea-get-client-missing-version` | warning | `server.get_client()` without `version=` (wrong daemon on dual-URL servers). |
| `kea-exception-detail-in-response` | error | `str(exc)` / f-string of a caught exception leaked into `messages.*` / HTTP / DRF responses. |
| `kea-command-result-indexed-without-guard` | error | `client.command(...)[0]` indexed directly, before validating the response shape. |

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
It scans the whole package. Install the hook types once:

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
