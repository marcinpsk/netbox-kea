# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for pytest configuration shared by unit and integration suites."""

from __future__ import annotations

import ast
import os
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: The Playwright suite. It must stay inside the path the integration job runs.
_BROWSER_SUITE = REPOSITORY_ROOT / "tests" / "ui"
#: The whole integration suite, whose fixtures read endpoint overrides from the environment.
_INTEGRATION_SUITE = REPOSITORY_ROOT / "tests"


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


def _workflow_job(workflow: str, name: str) -> str:
    """Return one workflow job, bounded by the next top-level job key."""
    marker = f"  {name}:\n"
    lines = workflow.splitlines(keepends=True)
    assert marker in lines, f"The {name} job was renamed or removed."
    job = "".join(lines[lines.index(marker) + 1 :])
    return re.split(r"\n {2}[A-Za-z0-9_-]+:\n", job, maxsplit=1)[0]


def test_documented_unit_test_targets_are_shell_safe():
    """Keep task-specific test targets usable when copied into a shell."""
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text()

    assert "TEST_DB_NAME=test_netbox_kea_review TEST_REDIS_HOST=netbox-kea-review-redis" in agents
    assert (
        "TEST_DB_NAME=test_netbox_kea_review TEST_REDIS_HOST=netbox-kea-review-redis \\\n"
        "  UPDATE_QUERY_COUNTS=1 uv run --native-tls pytest -n 1" in agents
    )
    assert "<unique-task>" not in agents
    assert "<dedicated-redis>" not in agents


def test_ci_configuration_writer_generates_the_requested_plugins(monkeypatch, tmp_path):
    """Generate an importable NetBox configuration with the requested plugins."""
    output = tmp_path / "configuration.py"
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/write_netbox_ci_configuration.py"),
            "--output",
            str(output),
            "--plugin",
            "netbox_kea",
            "--plugin",
            "netbox_dhcp",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for variable in ("REDIS_HOST", "REDIS_DATABASE", "REDIS_CACHE_HOST", "REDIS_CACHE_DATABASE"):
        monkeypatch.delenv(variable, raising=False)
    namespace = runpy.run_path(str(output))
    assert namespace["ALLOWED_HOSTS"] == ["*"]
    assert namespace["PLUGINS"] == ["netbox_kea", "netbox_dhcp"]
    assert namespace["DATABASE"] == {
        "NAME": "netbox",
        "USER": "netbox",
        "PASSWORD": "netbox",
        "HOST": "localhost",
        "PORT": "",
        "CONN_MAX_AGE": 300,
        "ENGINE": "django.db.backends.postgresql",
    }
    assert namespace["REDIS"] == {
        "tasks": {"HOST": "localhost", "PORT": 6379, "DATABASE": 0},
        "caching": {"HOST": "localhost", "PORT": 6379, "DATABASE": 1},
    }
    assert namespace["SECRET_KEY"] == "ci-test-secret-key-not-for-production-1234567890123456"
    assert namespace["API_TOKEN_PEPPERS"] == {0: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    assert namespace["PLUGINS_CONFIG"] == {"netbox_kea": {"kea_timeout": 30}}


def test_database_jobs_use_the_shared_ci_configuration_writer():
    """Configure both database-backed jobs through the tested writer."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    unit_test_job = re.sub(r"[ \t]*\\\n[ \t]*", " ", _workflow_job(workflow, "unit-test"))
    dhcp_plugin_job = re.sub(r"[ \t]*\\\n[ \t]*", " ", _workflow_job(workflow, "dhcp-plugin-test"))
    writer = 'python "${{ github.workspace }}/scripts/write_netbox_ci_configuration.py"'

    assert workflow.count(writer) == 2
    assert f"{writer} --output netbox/configuration.py --plugin netbox_kea\n" in unit_test_job
    assert f"{writer} --output netbox/configuration.py --plugin netbox_kea --plugin netbox_dhcp\n" in dhcp_plugin_job
    assert "cat > netbox/configuration.py" not in workflow


def test_workflow_job_slice_uses_structural_job_boundaries():
    """Find a job after reordering, and report a missing marker clearly."""
    workflow = "jobs:\n  lint:\n    marker: lint\n  unit-test:\n    marker: unit\n  release_1:\n    marker: release\n"
    nested_marker = "jobs:\n  lint:\n    unit-test:\n      marker: nested\n"

    assert _workflow_job(workflow, "unit-test") == "    marker: unit"
    with pytest.raises(AssertionError, match="missing job was renamed or removed"):
        _workflow_job(workflow, "missing")
    with pytest.raises(AssertionError, match="unit-test job was renamed or removed"):
        _workflow_job(nested_marker, "unit-test")


def test_dhcp_plugin_job_uses_the_unit_test_runtime_versions():
    """Keep both database-backed CI jobs on the same NetBox and Python inputs."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    unit_test_job = _workflow_job(workflow, "unit-test")
    dhcp_plugin_job = _workflow_job(workflow, "dhcp-plugin-test")

    for setting in ("ref", "python-version-file"):
        pattern = rf"^\s+{setting}: (.+)$"
        unit_value = re.search(pattern, unit_test_job, re.MULTILINE)
        plugin_value = re.search(pattern, dhcp_plugin_job, re.MULTILINE)
        assert unit_value is not None, (setting, unit_test_job)
        assert plugin_value is not None, (setting, dhcp_plugin_job)
        assert plugin_value.group(1) == unit_value.group(1), setting


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


def test_the_browser_suite_runs_in_the_integration_job():
    """The Playwright suite must sit inside the path CI actually executes.

    It previously lived in a top-level ``e2e/`` directory that no workflow named, so
    fifty browser tests never ran anywhere.
    """
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    integration_job = _workflow_job(workflow, "test")
    # The trailing space keeps the step's own "- name: Run pytest" line out.
    command = next(line for line in integration_job.splitlines() if "pytest " in line)
    # Every token that names a real path, so option order cannot change the answer.
    targets = [
        token
        for token in command.split("pytest ", 1)[1].split()
        if not token.startswith("-") and (REPOSITORY_ROOT / token).exists()
    ]
    assert any(_BROWSER_SUITE.is_relative_to(REPOSITORY_ROOT / target) for target in targets), (
        f"The integration job runs `pytest` over {targets}, which does not contain "
        f"{_BROWSER_SUITE.relative_to(REPOSITORY_ROOT)}."
    )


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

    release, patch = QUERY_COUNT_NETBOX_VERSION.rsplit(".", 1)
    different_patch_release = f"{release}.{int(patch) + 1}"

    assert query_counts_are_comparable(QUERY_COUNT_NETBOX_VERSION, update_mode=False)
    assert not query_counts_are_comparable(different_patch_release, update_mode=False)
    assert query_counts_are_comparable(different_patch_release, update_mode=True)


def _href_attribute_call(node: ast.AST) -> bool:
    """Is *node* a ``get_attribute("href")`` call, positional or by keyword?"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "get_attribute":
        return False
    named = [argument.value for argument in node.keywords if argument.arg == "name"]
    supplied = list(node.args[:1]) + named
    return any(isinstance(value, ast.Constant) and value.value == "href" for value in supplied)


def _goto_target(node: ast.AST) -> ast.expr | None:
    """Return the URL expression of a ``goto`` call, positional or by keyword."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != "goto":
        return None
    for argument in node.keywords:
        if argument.arg == "url":
            return argument.value
    return node.args[0] if node.args else None


def _unwrap(node: ast.expr) -> ast.expr:
    """Strip ``await`` and a walrus binding so every call spelling reads the same."""
    while isinstance(node, (ast.Await, ast.NamedExpr)):
        node = node.value
    return node


def _own_nodes(function: ast.AST):
    """Walk *function* without descending into a nested function or lambda."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _bindings(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """Return the ``(name, value)`` pairs one statement binds."""
    if isinstance(node, ast.Assign):
        return [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [(node.target.id, node.value)]
    if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
        return [(node.target.id, node.value)]
    return []


def _hrefs_navigated_raw(source: str) -> list[str]:
    """Return the places a function navigates straight to a raw ``get_attribute("href")``.

    ``get_attribute`` returns the raw attribute text, so a Django ``reverse()`` link is
    root-relative. The browser suite configures no Playwright ``base_url``, so navigating
    to that value fails. Read the resolved DOM ``href`` property instead.

    A name that is rebound anywhere in the function is left alone, so resolving the value
    before navigating is accepted rather than reported.
    """
    offenders: list[str] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound: dict[str, list[ast.expr]] = {}
        own = list(_own_nodes(function))
        for node in own:
            for name, value in _bindings(node):
                bound.setdefault(name, []).append(_unwrap(value))
        raw = {name for name, values in bound.items() if all(_href_attribute_call(v) for v in values)}
        for node in own:
            navigated = _goto_target(node)
            if navigated is None:
                continue
            target = _unwrap(navigated)
            if _href_attribute_call(target) or (isinstance(target, ast.Name) and target.id in raw):
                offenders.append(f"{function.name}:{node.lineno}")
    return offenders


def test_browser_navigation_resolves_hrefs_before_visiting_them():
    """A root-relative href cannot be navigated to without a Playwright base_url."""
    sources = sorted(_BROWSER_SUITE.rglob("*.py"))
    assert sources, "The browser suite moved; this guard would pass without reading anything."
    for path in sources:
        offenders = _hrefs_navigated_raw(path.read_text())
        assert not offenders, (
            f"{path.name} navigates to an unresolved get_attribute('href') value at {offenders}. "
            "Use the resolved DOM property, e.g. locator.evaluate('el => el.href')."
        )


def _environ_get_calls(node: ast.AST):
    """Yield every ``os.environ.get`` call in *node*."""
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call) or not isinstance(candidate.func, ast.Attribute):
            continue
        if candidate.func.attr != "get":
            continue
        target = candidate.func.value
        if isinstance(target, ast.Attribute) and target.attr == "environ":
            yield candidate


def _unsafe_environ_defaults(source: str) -> list[str]:
    """Return the environment overrides in *source* that a blank value can win.

    ``os.environ.get(NAME, default)`` returns the empty string when the variable is set
    but blank, so an exported-but-empty override beats the default. The suite's idiom is
    ``os.environ.get(NAME, "").strip() or default``, which treats blank as unset.
    """
    tree = ast.parse(source)
    # Safe is a positive shape, not the absence of known-bad ones: the call must read
    # ``get(...).strip() or <fallback>`` and at least one fallback must be something a
    # blank variable can actually fall through to. A bare ``.strip()`` still yields "",
    # and so does ``or ""``.
    blank_safe = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        left = node.values[0]
        if not isinstance(left, ast.Call) or not isinstance(left.func, ast.Attribute):
            continue
        if left.func.attr != "strip" or not isinstance(left.func.value, ast.Call):
            continue
        usable = [f for f in node.values[1:] if not (isinstance(f, ast.Constant) and not f.value)]
        if usable:
            blank_safe.add(id(left.func.value))
    offenders: list[str] = []
    for call in _environ_get_calls(tree):
        if len(call.args) < 2:
            continue  # no default: the caller handles None itself
        default = call.args[1]
        blank_default = isinstance(default, ast.Constant) and default.value == ""
        if not blank_default or id(call) not in blank_safe:
            variable = call.args[0]
            name = variable.value if isinstance(variable, ast.Constant) and isinstance(variable.value, str) else "?"
            offenders.append(f"{name} at line {call.lineno}")
    return offenders


def test_the_environ_default_guard_reads_every_spelling():
    """Table-test the guard: one that reports clean while missing the pattern is worse than none."""
    must_flag = (
        'os.environ.get("X", "http://default")',
        'os.environ.get("X", "")',
        'os.environ.get("X", "").lower() or "d"',
        'os.environ.get("X", "").strip()',  # no fallback: still "" for a blank variable
        'os.environ.get("X", "").strip() and "d"',
        'os.environ.get("X", "").strip() or ""',  # fallback is itself blank
        'os.environ.get("X", "").strip() or None',
    )
    must_not_flag = (
        'os.environ.get("X", "").strip() or "http://default"',
        'KeaClient(os.environ.get("X", "").strip() or "http://default")',
        'os.environ.get("X", "").strip() or f"http://localhost:{port}"',
        'os.environ.get("X", "").strip() or "" or "http://default"',
        'os.environ.get("X")',
        'config.get("X", "http://default")',
    )
    for source in must_flag:
        assert _unsafe_environ_defaults(source), f"the guard missed {source!r}"
    for source in must_not_flag:
        assert not _unsafe_environ_defaults(source), f"the guard wrongly flagged {source!r}"


def test_the_integration_suite_treats_a_blank_override_as_unset():
    sources = sorted(_INTEGRATION_SUITE.rglob("*.py"))
    assert sources, "The integration suite moved; this guard would pass without reading anything."
    for path in sources:
        offenders = _unsafe_environ_defaults(path.read_text())
        assert not offenders, (
            f"{path.relative_to(REPOSITORY_ROOT)} lets a blank environment value win at {offenders}. "
            'Use os.environ.get(NAME, "").strip() or <default>.'
        )


#: Every external tool the integration setup script calls. Stubbed so the script can run
#: in a test without a network, a Docker daemon, or a real Kea release tarball.
_SETUP_SCRIPT_TOOLS = ("openssl", "curl", "sha256sum", "tar", "docker")


def _write_tool_stubs(stub_bin: Path) -> None:
    """Write a stub for every external tool the setup script calls.

    Each stub drains stdin, because the real tools read it and a stub that exits first
    kills the writer of ``echo ... | sha256sum -c -`` with SIGPIPE.
    """
    stub_bin.mkdir(exist_ok=True)
    for tool in _SETUP_SCRIPT_TOOLS:
        stub = stub_bin / tool
        stub.write_text("#!/bin/sh\ncat >/dev/null 2>&1\nexit 0\n")
        stub.chmod(0o755)


def _run_setup_script(sandbox: Path, wheel_names: tuple[str, ...]) -> subprocess.CompletedProcess:
    """Run the real ``tests/test_setup.sh`` in *sandbox* with every external tool stubbed."""
    (sandbox / "tests" / "docker").mkdir(parents=True, exist_ok=True)
    (sandbox / "tests" / "test_setup.sh").write_bytes((REPOSITORY_ROOT / "tests/test_setup.sh").read_bytes())
    dist = sandbox / "dist"
    dist.mkdir(exist_ok=True)
    for name in wheel_names:
        (dist / name).write_text("not a real wheel")

    stub_bin = sandbox / "stub-bin"
    _write_tool_stubs(stub_bin)

    return subprocess.run(
        ["bash", "./tests/test_setup.sh"],
        cwd=sandbox,
        env={**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}", "NETBOX_CONTAINER_TAG": "v4.6"},
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def test_the_setup_script_runs_twice_on_one_checkout():
    """A second run must regenerate the certificates, not stop on the directory.

    The script created ``tests/docker/certs/`` without ``-p``, so every run after the
    first failed on a checkout that had already been set up once.
    """
    with tempfile.TemporaryDirectory() as directory:
        sandbox = Path(directory)
        first = _run_setup_script(sandbox, ("netbox_kea_ng-1.9.0-py3-none-any.whl",))
        assert first.returncode == 0, first.stderr
        second = _run_setup_script(sandbox, ("netbox_kea_ng-1.9.0-py3-none-any.whl",))
        assert second.returncode == 0, second.stderr
        assert (sandbox / "tests/docker/netbox_kea_ng-1.9.0-py3-none-any.whl").exists()


def test_the_setup_script_stubs_read_their_input():
    """The setup script pipes into ``sha256sum -c -``, so the stub must read stdin.

    A stub that exits without reading closes the pipe under the writer, which then dies
    of SIGPIPE and takes the whole script with it through ``pipefail``. A short payload
    fits in the pipe buffer and usually hides that, so send more than the buffer holds.
    """
    with tempfile.TemporaryDirectory() as directory:
        stub_bin = Path(directory) / "stub-bin"
        _write_tool_stubs(stub_bin)
        for tool in _SETUP_SCRIPT_TOOLS:
            result = subprocess.run(
                ["bash", "-o", "pipefail", "-c", f'head -c 262144 /dev/zero | "{stub_bin / tool}"'],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            assert result.returncode == 0, (
                f"The {tool} stub left the writer at {result.returncode}. "
                "128 means a signal, and 141 is SIGPIPE from a reader that never read."
            )


@pytest.mark.parametrize("wheel_names", [(), ("one-1.0.whl", "two-2.0.whl")])
def test_the_setup_script_refuses_an_ambiguous_wheel_set(wheel_names):
    """A stale wheel beside the new one once put two file names in one path.

    The copy then failed on a path naming both, which reads as a missing file rather
    than as a dirty ``dist/``.
    """
    with tempfile.TemporaryDirectory() as directory:
        result = _run_setup_script(Path(directory), wheel_names)

        assert result.returncode == 1, result.stdout
        assert "Expected exactly one wheel" in result.stderr, result.stderr
