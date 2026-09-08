# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The commit hook and CI must run the SAME ruff.

Two gates lint this tree: the ruff-pre-commit hook blocks the commit, and CI runs
`uv run ruff`, which resolves the version from uv.lock. When those drift they can
demand contradictory things. v0.15.8 reported BLE001 on a blind except whose body
logs the exception, while 0.16.4 treats it as handled and reports the noqa the
older version required as an unused RUF100: no tree satisfied both.

Dependabot updates the lock file and never touches the hook rev, so this has to
fail loudly rather than be kept in step by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = REPOSITORY_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
UV_LOCK = REPOSITORY_ROOT / "uv.lock"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


def _pre_commit_version(text: str | None = None) -> str:
    """Return the version the ruff-pre-commit hook is pinned to."""
    source = PRE_COMMIT.read_text(encoding="utf-8") if text is None else text
    found = re.search(
        r"repo: https://github\.com/astral-sh/ruff-pre-commit\s*\n\s*rev: v(\S+)",
        source,
    )
    assert found, "the ruff pre-commit hook has no rev"
    return found.group(1)


def _declared_version(text: str | None = None) -> str:
    """Return the exact version the dev dependency group pins ruff to."""
    source = PYPROJECT.read_text(encoding="utf-8") if text is None else text
    groups = tomllib.loads(source)["dependency-groups"]["dev"]
    [declared] = [entry for entry in groups if re.match(r"ruff\b", entry)]
    found = re.fullmatch(r"ruff==(\S+)", declared)
    assert found, (
        f"the dev group declares an unpinned ruff: {declared!r}. A range lets uv.lock move "
        "under the hook rev, which is the drift this test exists to stop."
    )
    return found.group(1)


def _locked_version() -> str:
    """Return the ruff version uv.lock resolves, which is what CI runs."""
    for entry in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
        if entry["name"] == "ruff":
            return entry["version"]
    raise AssertionError("ruff is absent from the lock file")


def _workflow_literal_versions() -> dict[str, str]:
    """Return any literal `ruff==` pin a workflow installs, keyed by file name.

    CI installs ruff through uv today, so this is normally empty. It guards the case
    where someone adds a literal pin later and it disagrees with the lock file.
    """
    versions = {}
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        found = re.findall(r"ruff==([0-9][^\s\"']*)", workflow.read_text(encoding="utf-8"))
        assert len(set(found)) <= 1, f"{workflow.name} installs different ruff versions: {found}"
        if found:
            versions[workflow.name] = found[0]
    return versions


def test_every_ruff_pin_names_one_version():
    """Every place that names a ruff version must name the same one."""
    versions = {
        ".pre-commit-config.yaml": _pre_commit_version(),
        "pyproject.toml": _declared_version(),
        "uv.lock": _locked_version(),
        **_workflow_literal_versions(),
    }

    assert len(set(versions.values())) == 1, f"ruff versions have drifted apart: {versions}"


def test_a_ranged_dev_dependency_is_rejected():
    """A range must fail: it is what let the lock file drift from the hook rev."""
    with pytest.raises(AssertionError, match="unpinned ruff"):
        _declared_version('[dependency-groups]\ndev = ["ruff>=0.8.0"]\n')


def test_a_missing_hook_rev_is_rejected():
    """A hook block without a rev must fail rather than read as "no opinion"."""
    with pytest.raises(AssertionError, match="no rev"):
        _pre_commit_version("repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    hooks: []\n")
