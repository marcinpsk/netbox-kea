<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# opengrep ruleset

Custom [opengrep](https://github.com/opengrep/opengrep) rules that encode this
project's `CLAUDE.md` security/correctness invariants as machine-checked gates,
so the same classes of bug stop coming back review after review.

## Why opengrep (and not just ruff / CodeQL)

- **ruff** (incl. the `S` / flake8-bandit rules) covers generic Python lint and
  security smells, but can't express project-specific call-shape rules.
- **CodeQL** (`.github/workflows/codeql.yml`) covers broad dataflow SAST.
- **opengrep** fills the gap: cheap, readable YAML patterns for *our* invariants
  — and it is the same engine **CodeRabbit** runs.

## Relationship to CodeRabbit (these run *on top* of CR's defaults)

CodeRabbit auto-detects an opengrep config **only** when it is named
`opengrep.yml` / `semgrep.yml` (and a few variants) — and when it finds one it
runs *that* **instead of** its default packs. We deliberately do **not** use
those names: the ruleset lives at **`.opengrep/kea-rules.yaml`**, so CodeRabbit
keeps running its own default opengrep packs, and these custom rules are enforced
*additionally* by the **pre-push hook** and the **CI `opengrep` job**. Net result:
CR's broad coverage **plus** our project-specific rules.

## Layout

| Path | Purpose |
| --- | --- |
| `.opengrep/kea-rules.yaml` | The ruleset. **Single source of truth.** Used by the pre-push hook and CI; intentionally not named so CodeRabbit auto-detects it. |
| `.opengrep/tests/*.py` | Annotated rule-test fixtures (`# ruleid:` must match, `# ok:` must not). |
| `scripts/opengrep-scan.sh` | Scan the source tree; used by the pre-push hook and CI. Exits non-zero on any finding. |
| `scripts/opengrep-test.sh` | Run the rule-tests against the ruleset. |

## Rules

| Rule id | Severity | Catches |
| --- | --- | --- |
| `kea-get-client-missing-version` | warning | `server.get_client()` without `version=` (wrong daemon on dual-URL servers). |
| `kea-exception-detail-in-response` | error | `str(exc)` / f-string of a caught exception leaked into `messages.*` / HTTP / DRF responses. |
| `kea-command-result-indexed-without-guard` | error | `client.command(...)[0]` indexed directly, before validating the response shape. |

## Running locally

```bash
# Scan (same as the pre-push hook):
./scripts/opengrep-scan.sh

# Run the rule-tests:
./scripts/opengrep-test.sh
```

Both scripts find opengrep via `$OPENGREP_BIN`, then `PATH`, then
`~/.local/opt/opengrep/bin`. Install opengrep from
<https://github.com/opengrep/opengrep> (or set `OPENGREP_BIN`).

## How it gates pushes

The `opengrep` hook in `.pre-commit-config.yaml` runs at the **pre-push** stage
(not on every commit). Install the hook types once:

```bash
pre-commit install --install-hooks
```

CI runs the same rules in the `opengrep` job (`.github/workflows/ci.yml`), so a
`--no-verify` push is still caught.

## Suppressing a true exception

Add an inline `# nosemgrep: <rule-id>` on the offending line, with a short reason
comment above it — see `views/server.py` (the version-agnostic Control Agent
status call).

## Adding a rule

1. Add the rule to `.opengrep/kea-rules.yaml`.
2. Add a fixture `.opengrep/tests/<rule-id>.py` with `# ruleid:` / `# ok:` lines.
3. `./scripts/opengrep-test.sh` — confirm it passes.
4. `./scripts/opengrep-scan.sh` — confirm the existing tree is clean (or fix it).
