# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for pytest configuration shared by unit and integration suites."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _pytest_worker_settings(text: str) -> list[dict[str, str]]:
    """Return the xdist settings of every pytest command in *text*, one entry per command.

    Shell line continuations are joined first, so a command spread over several lines is
    one entry. Values are whole tokens, because a substring test accepts ``-n 10`` for
    ``-n 1`` and ``--maxschedchunk=10`` for ``--maxschedchunk=1``.
    """
    settings = []
    for command in re.findall(r"\bpytest\b.*", text.replace("\\\n", " ")):
        tokens = command.split()
        entry: dict[str, str] = {}
        for index, token in enumerate(tokens):
            if token == "-n" and index + 1 < len(tokens):
                entry["workers"] = tokens[index + 1]
            elif token.startswith("--maxschedchunk="):
                entry["maxschedchunk"] = token.split("=", 1)[1]
        settings.append(entry)
    return settings


def test_documented_unit_test_targets_are_shell_safe():
    """Keep task-specific test targets usable when copied into a shell."""
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text()

    assert "TEST_DB_NAME=test_netbox_kea_review TEST_REDIS_HOST=netbox-kea-review-redis" in agents
    assert (
        "TEST_DB_NAME=test_netbox_kea_review TEST_REDIS_HOST=netbox-kea-review-redis \\\n"
        "  UPDATE_QUERY_COUNTS=1 uv run pytest -n 1" in agents
    )
    assert "<unique-task>" not in agents
    assert "<dedicated-redis>" not in agents


def test_worker_settings_are_read_as_whole_tokens():
    """Reject the near misses a substring test accepts: `-n 10` is not `-n 1`."""
    text = "pytest netbox_kea/tests/ \\\n  -n 10 --maxschedchunk=10 -q\npytest tests/ -n auto --maxschedchunk=1\n"

    assert _pytest_worker_settings(text) == [
        {"workers": "10", "maxschedchunk": "10"},
        {"workers": "auto", "maxschedchunk": "1"},
    ]


def test_ci_and_documented_commands_request_auto_workers():
    """Keep every entry point on `-n auto` so the conftest cap is the only ceiling.

    One worker stays allowed, because the query-count baseline must be recorded serially
    and a single-module job gains nothing from repeating the database setup.
    """
    workflow_workers = [
        entry["workers"]
        for entry in _pytest_worker_settings((REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text())
        if "workers" in entry
    ]

    assert "auto" in workflow_workers, workflow_workers
    assert set(workflow_workers) <= {"auto", "1"}, workflow_workers

    for relative_path in ("AGENTS.md", "README.md"):
        settings = _pytest_worker_settings((REPOSITORY_ROOT / relative_path).read_text())
        workers = [entry["workers"] for entry in settings if "workers" in entry]
        chunks = {entry["maxschedchunk"] for entry in settings if "maxschedchunk" in entry}

        assert "auto" in workers, (relative_path, workers)
        assert set(workers) <= {"auto", "1"}, (relative_path, workers)
        assert chunks == {"1"}, (relative_path, chunks)


def test_documented_integration_commands_disable_pytest_django():
    """Keep integration commands independent from the unit-test Django settings."""
    for relative_path in ("AGENTS.md", "README.md"):
        contents = (REPOSITORY_ROOT / relative_path).read_text()
        assert "pytest -p no:django tests/" in contents, relative_path


def test_documented_pull_request_pipeline_fails_closed():
    """Make the PR triage pipeline propagate failures from every stage."""
    issue_tracker = (REPOSITORY_ROOT / "docs/agents/issue-tracker.md").read_text()

    assert "set -euo pipefail\n  repo=" in issue_tracker


def test_context_uses_the_canonical_server_term():
    """Keep the Server definition consistent with its avoided terminology."""
    context = (REPOSITORY_ROOT / "CONTEXT.md").read_text()

    assert "A configured Kea server that provides DHCPv4, DHCPv6, or both." in context
    assert "A configured Kea endpoint" not in context


def test_documented_pull_request_pipeline_fetches_all_review_surfaces():
    """Fetch review bodies and paginated inline comments during PR triage."""
    issue_tracker = (REPOSITORY_ROOT / "docs/agents/issue-tracker.md").read_text()

    assert "--json number,title,body,labels,author,comments,reviews" in issue_tracker
    assert 'gh api --paginate "repos/$repo/pulls/$number/comments"' in issue_tracker


def test_serial_django_suite_is_rejected():
    """Reject serial unit runs before they can clear the manual environment's cache."""
    probe = REPOSITORY_ROOT / "netbox_kea" / "tests" / "test_parallel_test_setup.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(Path(entry or Path.cwd()).resolve()) for entry in sys.path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "--collect-only", "-q", "-p", "no:django"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 4
    assert "requires pytest-xdist" in result.stdout + result.stderr


def test_pytest_configuration_works_with_django_plugin_disabled():
    """Keep unit-only pytest options out of the integration test command."""
    with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as temporary_directory:
        placeholder_test = Path(temporary_directory) / "test_placeholder.py"
        placeholder_test.write_text("def test_placeholder():\n    pass\n")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(placeholder_test), "--collect-only", "-q", "-p", "no:django"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, result.stdout + result.stderr
