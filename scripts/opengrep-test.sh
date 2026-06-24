#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Run opengrep rule-tests for the ruleset (.opengrep/kea-rules.yaml) against the
# annotated fixtures in .opengrep/tests/. Each fixture carries `# ruleid:` /
# `# ok:` markers asserting which lines must (and must not) match.
#
# `opengrep test` pairs a <stem>.yaml rule file with a same-stem <stem>.py
# fixture inside one directory. To keep a single source of truth
# (.opengrep/kea-rules.yaml) we stage a temp directory pairing a copy of the
# ruleset with each fixture, then run the test there.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

opengrep_bin="${OPENGREP_BIN:-}"
if [[ -z "$opengrep_bin" ]]; then
  if command -v opengrep >/dev/null 2>&1; then
    opengrep_bin="$(command -v opengrep)"
  elif [[ -x "$HOME/.local/opt/opengrep/bin/opengrep" ]]; then
    opengrep_bin="$HOME/.local/opt/opengrep/bin/opengrep"
  else
    echo "error: opengrep not found. Install it from https://github.com/opengrep/opengrep" >&2
    echo "       (or set OPENGREP_BIN=/path/to/opengrep)." >&2
    exit 1
  fi
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for fixture in "$repo_root"/.opengrep/tests/*.py; do
  stem="$(basename "$fixture" .py)"
  cp "$repo_root/.opengrep/kea-rules.yaml" "$tmp/$stem.yaml"
  cp "$fixture" "$tmp/$stem.py"
done

exec "$opengrep_bin" test "$tmp"
