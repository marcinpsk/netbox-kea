# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the mypy gate (``scripts/mypy-gate.sh`` plus ``[tool.mypy]``).

The gate exists to catch annotation drift between a producer and its consumer, the class
of defect that reached review as ``list[dict | Reservation]`` passed to a
``list[Reservation]`` parameter. These tests prove the configuration still reports that
class, so weakening it (for example by disabling ``arg-type``) fails here rather than
silently retiring the gate.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPOSITORY_ROOT / "mypy-baseline.txt"
GATE = REPOSITORY_ROOT / "scripts/mypy-gate.sh"

DRIFTING_MODULE = '''
class Reservation:
    """One reservation."""


def consume(*, records: list[Reservation]) -> None:
    """Accept only reservations."""


def produce() -> None:
    """Fill one mixed list and hand it to a narrower parameter."""
    records: list[dict | Reservation] = []
    consume(records=records)
'''


def test_the_project_configuration_reports_annotation_drift(tmp_path):
    """`list` is invariant, so the mixed list must not satisfy `list[Reservation]`."""
    api = pytest.importorskip("mypy.api", reason="mypy is a dev dependency")
    module = tmp_path / "drift.py"
    module.write_text(DRIFTING_MODULE)

    stdout, _stderr, status = api.run(["--config-file", str(REPOSITORY_ROOT / "pyproject.toml"), str(module)])

    assert status != 0, f"mypy accepted the invariant-list call:\n{stdout}"
    assert "[arg-type]" in stdout, f"the arg-type check is disabled:\n{stdout}"


def test_a_matching_annotation_passes(tmp_path):
    """The same call is clean once the parameter names both record types."""
    api = pytest.importorskip("mypy.api", reason="mypy is a dev dependency")
    module = tmp_path / "aligned.py"
    module.write_text(DRIFTING_MODULE.replace("records: list[Reservation]", "records: list[dict | Reservation]"))

    stdout, _stderr, status = api.run(["--config-file", str(REPOSITORY_ROOT / "pyproject.toml"), str(module)])

    assert status == 0, f"mypy rejected an aligned call:\n{stdout}"


def test_the_baseline_records_only_pre_existing_errors():
    """Every baseline line is a real mypy finding, so the file cannot mute new codes."""
    # No lower bound on the count: test_resolving_every_baseline_error_passes_the_gate
    # requires that fixing the last error still builds, which leaves the baseline empty.
    entries = [line for line in BASELINE.read_text().splitlines() if line.strip()]
    for entry in entries:
        assert re.match(r"^netbox_kea/[\w/.]+:\d+: (error|note): .+", entry), f"Malformed baseline entry: {entry}"


def test_the_lint_job_and_the_pre_push_hook_share_the_gate():
    """CI and the local hook must run one command, or the two gates drift apart.

    Naming the same script is not enough. The gate resolves ``mypy`` from PATH, so a hook
    that skips ``uv run`` type-checks against whatever interpreter the push shell exposes.
    Without ``django-stubs`` from the locked dev group that report differs from CI.
    """
    import yaml

    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    hooks = yaml.safe_load((REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text())

    ci_commands = [
        line.split("run:", 1)[1].strip() for line in workflow.splitlines() if re.search(r"run:.*mypy-gate\.sh", line)
    ]
    hook_entries = [
        hook["entry"]
        for repository in hooks["repos"]
        for hook in repository["hooks"]
        if "mypy-gate.sh" in hook.get("entry", "")
    ]

    assert ci_commands, "The lint job no longer runs the mypy gate."
    assert hook_entries, "The pre-push hook no longer runs the mypy gate."
    assert set(ci_commands) == set(hook_entries), (
        f"CI runs {ci_commands} while the hook runs {hook_entries}; both must run one command."
    )
    assert GATE.stat().st_mode & 0o111, "scripts/mypy-gate.sh is not executable."


def test_the_pre_push_gate_cannot_be_skipped_by_file_type():
    """The gate reads the whole package, so no changed-file filter may gate it.

    A push that touches only mypy-baseline.txt, pyproject.toml or the gate script
    changes what the gate reports while matching no Python file.
    """
    import yaml

    hooks = yaml.safe_load((REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text())
    gate = [
        hook for repository in hooks["repos"] for hook in repository["hooks"] if "mypy-gate.sh" in hook.get("entry", "")
    ]

    assert len(gate) == 1, f"Expected one mypy gate hook, found {len(gate)}."
    hook = gate[0]
    filters = {key: hook[key] for key in ("types", "types_or", "files") if key in hook}
    assert hook.get("always_run") or not filters, (
        f"The gate hook is filtered by {filters} and is not always_run, so a push that "
        "changes only the baseline or the mypy config skips it."
    )


def _fake_tools(*, mypy_stdout: str, mypy_status: int):
    """Put a stub ``mypy`` on PATH so the gate's own control flow can be exercised.

    The real ``mypy-baseline`` stays in play, so the baseline comparison is genuine; only
    the type-checker is replaced, because these cases (a crash, a fully clean tree) cannot
    be produced on demand from the real source tree.
    """
    bin_dir = Path(tempfile.mkdtemp()) / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mypy"
    fake.write_text(f"#!/usr/bin/env bash\ncat <<'REPORT'\n{mypy_stdout}\nREPORT\nexit {mypy_status}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    return environment


def _run_gate(environment):
    return subprocess.run(
        [str(GATE)], cwd=REPOSITORY_ROOT, env=environment, capture_output=True, text=True, check=False
    )


# The gate shells out to the real mypy-baseline, so these cases need the dev group.
# CI installs it system-wide in the unit-test job; a bare checkout may not have it.
requires_baseline_tool = pytest.mark.skipif(
    shutil.which("mypy-baseline") is None, reason="mypy-baseline is a dev dependency"
)


@requires_baseline_tool
def test_a_crash_fails_the_gate_instead_of_reporting_success():
    """An empty report filters to "no new errors", so the exit status must be read."""
    result = _run_gate(_fake_tools(mypy_stdout="", mypy_status=2))

    assert result.returncode != 0, f"the gate passed while nothing was checked:\n{result.stdout}"
    assert "nothing was type-checked" in result.stderr, result.stderr


@requires_baseline_tool
def test_resolving_every_baseline_error_passes_the_gate():
    """Fixing type errors is an improvement and must not break the build."""
    result = _run_gate(_fake_tools(mypy_stdout="", mypy_status=0))

    assert result.returncode == 0, f"the gate failed for a clean tree:\n{result.stdout}{result.stderr}"


@requires_baseline_tool
def test_an_unbaselined_error_fails_the_gate():
    """A finding absent from the baseline is new work and must fail."""
    unknown = "netbox_kea/jobs.py:1: error: Nonexistent baseline finding for the gate test  [arg-type]"
    result = _run_gate(_fake_tools(mypy_stdout=unknown, mypy_status=1))

    assert result.returncode != 0, f"the gate accepted an unbaselined error:\n{result.stdout}"
    assert "Nonexistent baseline finding" in result.stdout, result.stdout


@requires_baseline_tool
def test_a_baselined_error_passes_the_gate():
    """A finding already recorded in the baseline is known debt, not a new failure."""
    # test_resolving_every_baseline_error_passes_the_gate accepts an empty baseline, so
    # indexing the first entry unconditionally would report an error on a clean tree.
    entries = [line for line in BASELINE.read_text().splitlines() if line.strip()]
    if not entries:
        pytest.skip("The baseline is empty, so there is no recorded finding to replay.")
    known = entries[0].replace(":0:", ":1:", 1)
    result = _run_gate(_fake_tools(mypy_stdout=known, mypy_status=1))

    assert result.returncode == 0, f"the gate rejected a baselined error:\n{result.stdout}{result.stderr}"
