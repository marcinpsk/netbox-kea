#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Run the repo's custom opengrep ruleset (.opengrep/rules) over the source tree.
# Used by the pre-push hook and CI to catch CLAUDE.md rule violations locally,
# before CodeRabbit's opengrep pass flags them on the PR.
#
# Locates opengrep via (1) $OPENGREP_BIN, (2) PATH, (3) the default user install
# dir (~/.local/opt/opengrep/bin), so it works even when opengrep is not on PATH.
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

# Scan explicit targets if given, otherwise the package source tree.
targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
  targets=("$repo_root/netbox_kea")
fi

exec "$opengrep_bin" scan \
  --config "$repo_root/.opengrep/kea-rules.yaml" \
  --error \
  "${targets[@]}"
