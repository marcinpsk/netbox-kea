#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Type-check netbox_kea and fail only on errors that are not in mypy-baseline.txt.
# Used by the pre-push hook and CI so both run the identical check.
#
# The baseline records the type errors that already existed when this gate was added.
# New errors fail the gate; fixing a baseline error and running --sync shrinks the file.
# mypy-baseline matches on the error text, not the line number, so unrelated edits above
# an error do not resync the baseline.
#
# NetBox is deliberately not installed here. It ships no type information, so its objects
# would be Any either way, and pinning a NetBox version would make this gate fail on an
# upstream release rather than on our own drift.
#
# Usage: scripts/mypy-gate.sh [--sync]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

baseline="$repo_root/mypy-baseline.txt"

# Locate each tool via (1) the env override, (2) PATH, (3) the project venv, so the hook
# works whether the caller is uv, an activated venv, or a bare shell.
find_tool() {
  local name="$1" override="$2" variable="$3"
  if [[ -n "$override" ]]; then echo "$override"; return; fi
  if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return; fi
  if [[ -x "$repo_root/.venv/bin/$name" ]]; then echo "$repo_root/.venv/bin/$name"; return; fi
  echo "error: $name not found. Install the dev group (uv sync --group dev)," >&2
  echo "       or set $variable=/path/to/$name." >&2
  exit 1
}

mypy_bin="$(find_tool mypy "${MYPY_BIN:-}" MYPY_BIN)"
baseline_bin="$(find_tool mypy-baseline "${MYPY_BASELINE_BIN:-}" MYPY_BASELINE_BIN)"

# mypy exits 0 with no findings and 1 when it reports them. Anything higher is a crash or
# a bad configuration, which must fail loudly: an empty report would otherwise filter to
# "no new errors" and the gate would pass while nothing was checked.
run_mypy() {
  local status=0
  report="$("$mypy_bin" --no-error-summary)" || status=$?
  if (( status > 1 )); then
    echo "error: mypy exited with status $status; nothing was type-checked." >&2
    exit "$status"
  fi
}

if [[ "${1:-}" == "--sync" ]]; then
  run_mypy
  printf '%s\n' "$report" | "$baseline_bin" sync --baseline-path "$baseline"
  echo "mypy-gate: baseline written to $baseline"
  exit 0
fi

if [[ ! -f "$baseline" ]]; then
  echo "error: $baseline is missing. Generate it with scripts/mypy-gate.sh --sync." >&2
  exit 1
fi

# --allow-unsynced: a resolved baseline error must not fail the gate, or fixing a type
# error would break the build. Run --sync to prune the baseline after fixing one.
run_mypy
printf '%s\n' "$report" | "$baseline_bin" filter --baseline-path "$baseline" --allow-unsynced
