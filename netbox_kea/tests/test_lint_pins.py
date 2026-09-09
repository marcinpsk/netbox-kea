# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""One source names the ruff version, and both lint gates derive it from there.

Two gates lint this tree: the pre-commit hook blocks the commit, and CI runs ruff
through uv. When they resolve different versions they can demand contradictory
things. v0.15.8 reported BLE001 on a blind except whose body logs the exception,
while 0.16.4 treats it as handled and reports the noqa the older version required
as an unused RUF100: no tree satisfied both.

Comparing several pins for equality is the wrong guard. Dependabot updates
pyproject and uv.lock and nothing else, so any second hardcoded version turns
every bump red. These tests instead hold that pyproject's dev group plus uv.lock
are the ONLY place a version appears, and that the hook and the workflow both go
through uv. A bump of that single source then keeps every gate in step by itself.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import tomllib
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = REPOSITORY_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
UV_LOCK = REPOSITORY_ROOT / "uv.lock"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


def _declared_version(text: str | None = None) -> str:
    """Return the version the dev dependency group pins ruff to."""
    source = PYPROJECT.read_text(encoding="utf-8") if text is None else text
    groups = tomllib.loads(source)["dependency-groups"]["dev"]
    [declared] = [entry for entry in groups if re.match(r"ruff\b", entry)]
    found = re.fullmatch(r"ruff==(\S+)", declared)
    assert found, (
        f"the dev group declares an unpinned ruff: {declared!r}. It is the single source, "
        "so it has to name one version for the lock file to follow."
    )
    return found.group(1)


def _locked_version() -> str:
    """Return the ruff version uv.lock resolves, which is what both gates run."""
    for entry in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
        if entry["name"] == "ruff":
            return entry["version"]
    raise AssertionError("ruff is absent from the lock file")


def _local_ruff_hooks(text: str | None = None) -> dict[str, list[str]]:
    """Return each local ruff hook's command, having checked it runs through uv."""
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8") if text is None else text)

    pinned = [
        repository
        for repository in config["repos"]
        if repository["repo"] == "https://github.com/astral-sh/ruff-pre-commit"
    ]
    assert not pinned, (
        "the ruff-pre-commit repo carries its own rev, which dependabot never updates. "
        "Run ruff from a local hook through uv instead."
    )

    hooks = {
        hook["id"]: hook
        for repository in config["repos"]
        if repository["repo"] == "local"
        for hook in repository["hooks"]
        if hook["id"] in {"ruff-check", "ruff-format"}
    }
    assert set(hooks) == {"ruff-check", "ruff-format"}, f"expected local ruff hooks, found {sorted(hooks)}"

    commands = {}
    for hook_id, hook in hooks.items():
        command = shlex.split(hook["entry"])
        assert command[:2] == ["uv", "run"], f"the {hook_id} hook must run ruff through uv: {hook['entry']!r}"
        assert "ruff" in command, f"the {hook_id} hook does not run ruff: {hook['entry']!r}"
        assert hook["language"] == "system", f"the {hook_id} hook must use the system language"
        commands[hook_id] = command
    return commands


def _files_naming_a_ruff_version() -> list[str]:
    """Return every file outside the single source that hardcodes a ruff version."""
    candidates = [PRE_COMMIT, *sorted(WORKFLOWS.glob("*.y*ml"))]
    return [path.name for path in candidates if re.search(r"ruff==", path.read_text(encoding="utf-8"))]


def test_the_dev_pin_and_the_lock_file_agree():
    """The single source has to be self-consistent."""
    assert _declared_version() == _locked_version()


def test_nothing_outside_the_single_source_names_a_ruff_version():
    """A second hardcoded version is what makes a dependabot bump land half-applied."""
    offenders = _files_naming_a_ruff_version()
    assert offenders == [], f"these files pin ruff themselves instead of deriving it from uv.lock: {offenders}"


def test_the_commit_hook_runs_ruff_through_uv():
    """Only then does the hook resolve the same version CI does."""
    commands = _local_ruff_hooks()
    assert commands["ruff-check"][-2:] == ["--fix", "--exit-non-zero-on-fix"]
    assert commands["ruff-format"][-1] == "format"


def test_ci_runs_ruff_through_uv():
    """CI must not install its own ruff alongside the locked one."""
    runs = [
        step["run"]
        for workflow in sorted(WORKFLOWS.glob("*.y*ml"))
        for job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str) and re.search(r"(?<![\w-])ruff(?![\w-])", step["run"])
    ]
    assert runs, "no workflow runs ruff"
    for run in runs:
        assert re.match(r"uv run\b", run.strip()), f"a workflow runs ruff outside uv: {run!r}"


def test_a_ranged_dev_dependency_is_rejected():
    """A range would let the lock file move without the dev group recording it."""
    with pytest.raises(AssertionError, match="unpinned ruff"):
        _declared_version('[dependency-groups]\ndev = ["ruff>=0.8.0"]\n')


def test_a_pinned_ruff_pre_commit_repo_is_rejected():
    """The exact shape this repo used to have, and that broke on every bump."""
    with pytest.raises(AssertionError, match="dependabot never updates"):
        _local_ruff_hooks(
            "repos:\n"
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.16.4\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
        )


def test_a_hook_bypassing_uv_is_rejected():
    """A bare `ruff` entry would use whatever is on PATH, not the locked version."""
    with pytest.raises(AssertionError, match="must run ruff through uv"):
        _local_ruff_hooks(
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
            "        entry: ruff check\n"
            "        language: system\n"
            "      - id: ruff-format\n"
            "        entry: uv run ruff format\n"
            "        language: system\n"
        )
