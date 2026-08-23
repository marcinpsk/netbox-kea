# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for pytest configuration shared by unit and integration suites."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


SERIAL_BY_DESIGN = "1"
"""The one worker count a unit-test command may request instead of ``auto``."""


def _pytest_commands(text: str) -> list[str]:
    """Return every pytest invocation in *text*, one string per command.

    Shell line continuations are joined first, so a command spread over several lines is
    one entry. The whitespace lookahead keeps prose such as ``pytest-xdist`` out.
    """
    return re.findall(r"pytest(?=[ \t]).*", text.replace("\\\n", " "))


def _xdist_settings(command: str) -> dict[str, str]:
    """Return the xdist settings of one pytest *command*, as whole tokens.

    A command that requests no worker count yields an empty mapping, so a lost ``-n``
    is visible instead of being filtered out. Values are whole tokens, because a
    substring test accepts ``-n 10`` for ``-n 1``.
    """
    tokens = command.split()
    settings: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token == "-n" and index + 1 < len(tokens):
            settings["workers"] = tokens[index + 1]
        elif token.startswith("--maxschedchunk="):
            settings["maxschedchunk"] = token.split("=", 1)[1]
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


def test_dhcp_plugin_job_uses_the_unit_test_runtime_versions():
    """Keep both database-backed CI jobs on the same NetBox and Python inputs."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    dhcp_plugin_job = workflow.split("  dhcp-plugin-test:\n", 1)[1].split("\n  lint:\n", 1)[0]

    assert "ref: v4.6.8" in dhcp_plugin_job
    assert "python-version-file: pyproject.toml" in dhcp_plugin_job
    assert "ref: v4.6.7" not in dhcp_plugin_job
    assert 'python-version: "3.12"' not in dhcp_plugin_job


def test_get_client_view_tests_pin_plugin_settings():
    """Keep each mutation-view test class independent from ambient plugin settings."""
    path = REPOSITORY_ROOT / "netbox_kea/tests/test_reservation_mutation_views.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    required_classes = {
        "TestReservationMutationViews",
        "TestReservationDocumentImport",
        "TestMutationCapabilityGate",
    }
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in required_classes
    }

    assert classes.keys() == required_classes
    for name, node in classes.items():
        decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
        assert "override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)" in decorators, name


def test_worker_settings_are_read_as_whole_tokens():
    """Reject the near misses a substring test accepts, and keep commands without `-n`."""
    text = (
        "pytest netbox_kea/tests/ \\\n  -n 10 --maxschedchunk=10 -q\n"
        "pytest tests/ -n auto --maxschedchunk=1\n"
        "pytest tests/ -p no:django -v\n"
        "pytest-xdist is a dependency, not a command\n"
    )

    commands = _pytest_commands(text)

    assert len(commands) == 3, commands
    assert [_xdist_settings(command) for command in commands] == [
        {"workers": "10", "maxschedchunk": "10"},
        {"workers": "auto", "maxschedchunk": "1"},
        {},
    ]


def test_every_unit_test_command_declares_auto_workers():
    """Require an explicit worker count on each unit-test command.

    Dropping `-n auto` from one command must fail here, so no command is skipped for
    lacking a worker count. Integration commands never use xdist and are excluded by
    their `-p no:django` marker. One worker stays allowed, because the query-count
    baseline must be recorded serially.
    """
    for relative_path in (".github/workflows/ci.yml", "AGENTS.md", "README.md"):
        text = (REPOSITORY_ROOT / relative_path).read_text()
        unit_commands = [command for command in _pytest_commands(text) if "-p no:django" not in command]
        assert unit_commands, relative_path

        workers = []
        for command in unit_commands:
            settings = _xdist_settings(command)
            assert "workers" in settings, (relative_path, command)
            assert settings["workers"] in {"auto", SERIAL_BY_DESIGN}, (relative_path, command)
            # CI distributes with `--dist loadscope` instead; the documented commands
            # keep one test per chunk so a worker never waits on a long scope.
            if settings["workers"] == "auto" and relative_path.endswith(".md"):
                assert settings.get("maxschedchunk") == "1", (relative_path, command)
            workers.append(settings["workers"])

        assert "auto" in workers, (relative_path, workers)


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


def test_dhcp_plugin_ci_uses_xdist():
    """Keep the DHCP plugin job on exactly one xdist worker, not merely on a count."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    marker = "- name: Run DHCP plugin adapter tests"
    assert marker in workflow, "The DHCP plugin adapter step was renamed or removed."
    # Bound the slice on the next step, so reformatting the workflow cannot widen it.
    command = re.split(r"\n\s*- name:", workflow.split(marker, 1)[1], maxsplit=1)[0]

    assert [_xdist_settings(entry) for entry in _pytest_commands(command)] == [{"workers": "1", "maxschedchunk": "1"}]


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
    # Same release as the line above, so only update_mode can explain the difference.
    assert query_counts_are_comparable(f"{QUERY_COUNT_NETBOX_VERSION}-Docker-5.0.2", update_mode=True)
