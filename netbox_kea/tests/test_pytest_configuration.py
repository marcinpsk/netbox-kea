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


def test_the_pinned_netbox_release_is_documented_only_by_its_constant():
    """Name the release in one place. A copy in the docs goes stale on the next bump."""
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text()

    assert "QUERY_COUNT_NETBOX_VERSION" in agents
    pinned = re.search(r"NetBox \*{0,2}v?\d+\.\d+\.\d+", agents)
    assert pinned is None, f"AGENTS.md names a NetBox patch release directly: {pinned.group(0)!r}"


def test_ci_pins_the_netbox_release_the_query_counts_describe():
    """Keep the unit-test NetBox checkout on the release the baselines were recorded on.

    The two drift silently otherwise: CI would assert counts from another release, and
    the conftest guard would skip the assertions on the pinned one.
    """
    from netbox_kea.tests.conftest import QUERY_COUNT_NETBOX_VERSION

    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    checkout = workflow.split("repository: netbox-community/netbox", 1)[1].split("path: netbox", 1)[0]

    assert f"ref: v{QUERY_COUNT_NETBOX_VERSION}\n" in checkout, checkout


def test_query_count_assertion_sites_still_exist():
    """Fail when NetBox moves the helper the conftest guard patches.

    A renamed import would make the guard patch nothing, so every release but the
    recorded one would start failing on counts again with no sign of why.
    """
    import importlib

    from netbox_kea.tests.conftest import QUERY_COUNT_ASSERTION_SITES

    for module_name in QUERY_COUNT_ASSERTION_SITES:
        module = importlib.import_module(module_name)
        assert hasattr(module, "assert_expected_query_count"), module_name


def test_query_counts_compare_only_on_the_recorded_release():
    """Compare on the recorded release and while recording, and nowhere else."""
    from netbox_kea.tests.conftest import QUERY_COUNT_NETBOX_VERSION, query_counts_are_comparable

    assert query_counts_are_comparable(QUERY_COUNT_NETBOX_VERSION, update_mode=False)
    assert not query_counts_are_comparable(f"{QUERY_COUNT_NETBOX_VERSION}-Docker-5.0.2", update_mode=False)
