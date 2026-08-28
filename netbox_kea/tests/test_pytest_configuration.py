# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for pytest configuration shared by unit and integration suites."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import runpy
import subprocess
import sys
import tempfile
import warnings
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


#: Documentation a reader outside this machine consumes.
_PUBLISHED_DOCS = ("AGENTS.md", "README.md")

#: Hosts and database names that are safe to publish: they name no particular machine
#: and paste into a shell unchanged.
_DOCUMENTED_TEST_ENV = "TEST_DB_NAME=test_netbox_kea_local TEST_REDIS_HOST=localhost"


def test_documented_unit_test_targets_are_shell_safe():
    """Keep the documented test command usable when copied into a shell.

    An angle-bracket placeholder is a redirect to the shell, so the pasted command
    fails with a syntax error rather than a clear "set this variable" message.
    """
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text()

    assert _DOCUMENTED_TEST_ENV in agents
    assert f"{_DOCUMENTED_TEST_ENV} \\\n  UPDATE_QUERY_COUNTS=1 uv run --native-tls pytest -n 1" in agents
    assert not re.search(r"TEST_(?:DB_NAME|REDIS_HOST)=<", agents), (
        "An angle-bracket placeholder is a shell redirect; name a real, generic default."
    )


def test_published_docs_name_no_machine_specific_resource():
    """Docs ship to readers who do not have this host's containers or databases.

    A documented value that only resolves on one machine sends every other reader to
    a connection error, and it discloses how that machine is set up.
    """
    # Hosts and database names that only exist on a particular developer machine.
    private = re.compile(r"[\w.-]*(?:review-redis|_review\b|devcontainer-devcontainer|nblp-)[\w.-]*")
    for name in _PUBLISHED_DOCS:
        text = (REPOSITORY_ROOT / name).read_text()
        assert text, f"{name} is empty; this guard would pass without reading anything."
        found = private.findall(text)
        assert not found, f"{name} names machine-specific resources: {sorted(set(found))}"


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
    commands = _pytest_commands(integration_job)
    assert commands, "The integration job no longer runs pytest."
    # Every token that names a real path, so option order cannot change the answer.
    targets = [
        token for token in commands[0].split()[1:] if not token.startswith("-") and (REPOSITORY_ROOT / token).exists()
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
    # Every job that checks NetBox out, so a bump cannot leave one job on an older release.
    checkouts = [
        section.split("path: netbox", 1)[0] for section in workflow.split("repository: netbox-community/netbox")[1:]
    ]

    assert checkouts, "No job checks NetBox out; this guard would pass without reading anything."
    for checkout in checkouts:
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


_COMPOSE_FILE = REPOSITORY_ROOT / "tests" / "docker" / "docker-compose.yml"

#: ``${NAME}``, ``${NAME:-default}`` and ``${NAME:+alternate}``. The argument excludes
#: braces, so a nested expansion is only matched once its inner form has been replaced.
_INTERPOLATION = re.compile(r"\$\{(\w+)(?::([-+])([^{}]*))?\}")


def _expand(value: str, variables: dict[str, str]) -> str:
    """Expand one Compose value the way Compose does, under a given environment.

    Innermost first, so ``${NAME:+${NAME},}`` resolves rather than being left literal.
    """

    def replace(match: re.Match) -> str:
        name, form, argument = match.groups()
        set_and_non_empty = bool(variables.get(name))
        if form is None:
            return variables.get(name, "")
        if form == "-":
            return variables[name] if set_and_non_empty else argument
        return argument if set_and_non_empty else ""

    while (expanded := _INTERPOLATION.sub(replace, value)) != value:
        value = expanded
    return value


def _empty_list_elements(source: str) -> list[str]:
    """Return every comma-separated Compose value that keeps an empty element.

    A list built as ``"${NAME:-},a,b"`` starts with a separator whenever NAME is unset.
    The variable must carry its own separator instead: ``"${NAME:+${NAME},}a,b"``.
    """
    offenders: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        key, separator, raw = line.strip().partition(": ")
        if not separator or "," not in raw or "${" not in raw:
            continue
        names = {match[0] for match in _INTERPOLATION.findall(raw)}
        unset = dict.fromkeys(names, "")
        given = dict.fromkeys(names, "set.example.invalid")
        if any("" in _expand(raw.strip('"'), setting).split(",") for setting in (unset, given)):
            offenders.append(f"{key} at line {number}")
    return offenders


def test_the_empty_list_element_guard_reads_both_environments():
    must_flag = (
        'NO_PROXY: "${NO_PROXY:-},localhost,nginx"',
        # The separator sits outside the expansion, so an unset variable still leaves it.
        'NO_PROXY: "${NO_PROXY:+${NO_PROXY}},localhost,nginx"',
        # Set but trailing: an empty last element.
        'NO_PROXY: "localhost,nginx,${NO_PROXY:-}"',
    )
    must_not_flag = (
        'NO_PROXY: "${NO_PROXY:+${NO_PROXY},}localhost,nginx"',
        'DB_NAME: "${DB_NAME:-netbox}"',
        "HOUSEKEEPING_INTERVAL: 86400",
    )
    for source in must_flag:
        assert _empty_list_elements(source), f"the guard missed {source!r}"
    for source in must_not_flag:
        assert not _empty_list_elements(source), f"the guard wrongly flagged {source!r}"


def test_the_compose_stack_builds_no_empty_list_element():
    """An unset proxy variable must leave the list unchanged, not add a blank entry."""
    assert _COMPOSE_FILE.exists(), "The compose stack moved; this guard reads nothing."
    offenders = _empty_list_elements(_COMPOSE_FILE.read_text())
    assert not offenders, (
        f"{_COMPOSE_FILE.relative_to(REPOSITORY_ROOT)} keeps an empty list element at {offenders}. "
        "Move the separator inside the expansion: ${NAME:+${NAME},}."
    )


def _unscoped_deletes(source: str) -> list[str]:
    """Return every ``.delete()`` whose receiver names a whole collection.

    ``x.all(...).delete()`` removes every object of that type in the target NetBox, so
    an integration fixture pointed at a real instance destroys data it never created.
    A receiver that filters (``.filter(...)``, ``.get(...)``) is bounded and passes.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "delete":
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "all":
            offenders.append(f"line {node.lineno}")
    return offenders


def test_the_unscoped_delete_guard_reads_the_receiver():
    must_flag = (
        "nb.users.permissions.all(0).delete()",
        "nb.ipam.ip_addresses.all().delete()",
    )
    must_not_flag = (
        "nb.users.permissions.filter(name=user).delete()",
        "nb.users.users.get(username=user).delete()",
        "obj.delete()",
        # Reading a whole collection is not a deletion.
        "everything = nb.users.permissions.all(0)",
    )
    for source in must_flag:
        assert _unscoped_deletes(source), f"the guard missed {source!r}"
    for source in must_not_flag:
        assert not _unscoped_deletes(source), f"the guard wrongly flagged {source!r}"


def test_the_integration_suite_deletes_only_what_it_created():
    """A fixture must never empty a collection it does not own.

    The browser fixtures accept ``NETBOX_URL``, so the target is not always the
    disposable Compose stack, and there is no rollback.
    """
    sources = sorted(_INTEGRATION_SUITE.rglob("*.py"))
    assert sources, "The integration suite moved; this guard would pass without reading anything."
    for path in sources:
        offenders = _unscoped_deletes(path.read_text())
        assert not offenders, (
            f"{path.relative_to(REPOSITORY_ROOT)} deletes a whole collection at {offenders}. "
            "Filter the queryset down to the objects the fixture created."
        )


#: Type aliases that describe one shared fact and must have exactly one definition.
#: The "Value" suffix keeps them apart from netaddr's same-named classes, which this
#: package also imports and which are not interchangeable with the stdlib ones.
_SHARED_ALIASES = ("Family", "IPAddressValue", "IPNetworkValue")


def _alias_definitions(alias: str) -> dict[str, int]:
    """Return each package module that assigns *alias* at module level, and its line."""
    found: dict[str, int] = {}
    for path in sorted((REPOSITORY_ROOT / "netbox_kea").rglob("*.py")):
        if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
            continue
        for node in ast.parse(path.read_text()).body:
            targets = node.targets if isinstance(node, ast.Assign) else []
            if any(isinstance(t, ast.Name) and t.id == alias for t in targets):
                found[str(path.relative_to(REPOSITORY_ROOT))] = node.lineno
    return found


@pytest.mark.parametrize("alias", _SHARED_ALIASES)
def test_each_shared_type_alias_is_defined_once(alias):
    """Two identical aliases in two modules can drift apart without any error.

    ``Family`` was written out in both the Reservation domain and the Subnet
    Catalogue. Both describe the same two DHCP families, so they are one fact.
    """
    definitions = _alias_definitions(alias)
    assert definitions, f"{alias} is defined nowhere; this guard would read nothing."
    assert len(definitions) == 1, f"{alias} is defined in {definitions}; import it from netbox_kea.constants."
    assert "netbox_kea/constants.py" in definitions, (
        f"{alias} is defined in {definitions}; netbox_kea/constants.py is where shared facts live."
    )


def test_ci_lints_every_file_the_pre_commit_hooks_lint():
    """The ruff pre-commit hooks carry no file filter, so they cover the whole repo.

    CI scoped to one package let a file outside it drift until a hook ran locally.
    Neither gate may be narrower than the other; ``[tool.ruff] exclude`` is the one
    place that decides what is out of scope.
    """
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    targets = re.findall(r"run: uv run ruff (?:check|format --check) (\S+)", workflow)
    assert targets, "The lint job no longer runs ruff; this guard would read nothing."
    assert set(targets) == {"."}, (
        f"CI runs ruff over {sorted(set(targets))}, but the pre-commit hooks read every "
        "Python file in the repository. Narrow the scope in [tool.ruff] exclude instead."
    )


def test_published_docs_run_ruff_over_the_scope_ci_lints():
    """A narrower documented scope lets a contributor pass locally and fail CI.

    The documented commands are the ones a contributor runs before pushing, so they
    must read the same files as the lint job.
    """
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    ci_targets = set(re.findall(r"run: uv run ruff (?:check|format --check) (\S+)", workflow))
    assert ci_targets, "The lint job no longer runs ruff; this guard would read nothing."

    for name in _PUBLISHED_DOCS:
        documented = re.findall(
            r"uv run ruff (?:check|format)(?: --check)? (\S+)", (REPOSITORY_ROOT / name).read_text()
        )
        assert documented, f"{name} documents no ruff command; this guard would read nothing."
        assert set(documented) == ci_targets, (
            f"{name} runs ruff over {sorted(set(documented))}, but CI reads {sorted(ci_targets)}."
        )


def _compose_service_names(source: str) -> set[str]:
    """Return the service keys the Compose file declares."""
    body = source.split("\nservices:\n", 1)[-1]
    # Stop at the next top-level key, or volumes and secrets would count as services.
    block = re.split(r"^\w", body, maxsplit=1, flags=re.MULTILINE)[0]
    return {line.split(":", 1)[0].strip() for line in block.splitlines() if re.match(r"^  [\w-]+:", line)}


def test_the_container_log_helper_names_a_service_the_stack_runs():
    """The helper returns "" for every failure, so a wrong name reads as a clean log.

    A default that matches no container turns every assertion on its result into a
    tautology.
    """
    source = (_BROWSER_SUITE / "test_workflows.py").read_text()
    default = re.search(r'def _tail_container_logs\(service: str = "([\w-]+)"', source)
    assert default, "The container-log helper moved; this guard would pass without reading anything."

    services = _compose_service_names(_COMPOSE_FILE.read_text())
    assert services, "The Compose file declares no service; this guard would pass without reading anything."
    assert default.group(1) in services, (
        f"_tail_container_logs reads the {default.group(1)!r} service, which is not one of {sorted(services)}."
    )


#: Helpers that submit a form in the browser suite. An add route plus one of these is
#: what creates live Kea state, whatever the test is called.
_SUBMIT_HELPERS = frozenset({"_submit_form_by_field", "_submit_and_wait_nav"})


def _called_names(body: list[ast.stmt]) -> set[str]:
    """Return every function or method name called anywhere inside *body*."""
    names: set[str] = set()
    for statement in body:
        for inner in ast.walk(statement):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if isinstance(name, str):
                names.add(name)
    return names


def _creates_live_kea_state(body: list[ast.stmt]) -> bool:
    """Report whether *body* navigates an add route and submits the form there."""
    called = _called_names(body)
    return any(name.endswith("_add_url") for name in called) and bool(called & _SUBMIT_HELPERS)


def _cycle_tests_without_teardown(source: str) -> list[str]:
    """Return every test that creates live Kea state without tearing it down.

    Naming alone missed `test_full_crud_lifecycle`, which creates a Reservation and only
    deleted it on the happy path. Requiring the creation calls inside the guarded ``try``
    body is what makes the check real: a submit placed before the ``try`` is not covered
    by its ``finally``, so a test that leaks would otherwise read as safe.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        guarded = [inner for inner in ast.walk(node) if isinstance(inner, ast.Try) and inner.finalbody]
        if _creates_live_kea_state(node.body):
            if not any(_creates_live_kea_state(block.body) for block in guarded):
                offenders.append(node.name)
        elif node.name.endswith("_add_and_delete_cycle") and not guarded:
            # Builds its Kea state through another route; the name is all there is to go on.
            offenders.append(node.name)
    return offenders


def test_every_live_kea_cycle_test_tears_its_object_down():
    """Kea rejects a duplicate, so one leftover object fails every later run.

    These tests write to a live daemon that nothing rolls back.
    """
    source = (_BROWSER_SUITE / "test_workflows.py").read_text()
    assert "_add_and_delete_cycle" in source, "The cycle tests moved; this guard would read nothing."
    assert _SUBMIT_HELPERS & set(re.findall(r"def (_submit\w+)", source)), (
        "The submit helpers were renamed; the behavioural half of this guard would read nothing."
    )
    offenders = _cycle_tests_without_teardown(source)
    assert not offenders, f"{offenders} create live Kea state without a finally that removes it."


#: A test whose submit runs before the ``try``, so its ``finally`` cannot undo it.
_CREATION_BEFORE_TRY = """
class TestLeak:
    def test_thing_add_and_delete_cycle(self):
        page.goto(self._reservation_add_url(base, 1))
        self._submit_form_by_field(page, "id_x")
        try:
            assert True
        finally:
            self._cleanup()
"""


def test_the_teardown_guard_requires_creation_inside_the_try():
    """A `finally` only undoes what the matching `try` body ran.

    Accepting any `try`/`finally` in the function would pass a test that submits the
    add form first and leaks the object on failure.
    """
    assert _cycle_tests_without_teardown(_CREATION_BEFORE_TRY) == ["test_thing_add_and_delete_cycle"]


def _unguarded_finally_cleanups(source: str) -> list[str]:
    """Return every helper a ``finally`` block calls that can raise out of itself."""
    tree = ast.parse(source)
    helpers = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        for statement in node.finalbody:
            for inner in ast.walk(statement):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if not isinstance(name, str):
                    continue
                helper = helpers.get(name)
                if helper is None:
                    continue
                if not any(isinstance(x, ast.Try) and x.handlers for x in ast.walk(helper)):
                    offenders.append(name)
    return sorted(set(offenders))


def test_every_finally_cleanup_reports_its_own_failure():
    """An exception raised in ``finally`` replaces the assertion that failed the test.

    These helpers talk to a live Kea daemon, so a teardown failure is exactly when the
    real failure matters most. They must warn instead of raise.
    """
    source = (_BROWSER_SUITE / "test_workflows.py").read_text()
    assert "finally:" in source, "The cycle tests no longer tear down; this guard would read nothing."
    offenders = _unguarded_finally_cleanups(source)
    assert not offenders, f"{offenders} run from finally and can replace the test's own failure."


class _FakeRowLocator:
    """The subset of the Playwright Locator API the reservation cleanup helper uses."""

    def __init__(self, rows: int, href: str = "http://netbox.invalid/reservations4/1/delete/"):
        self._rows = rows
        self._href = href

    def count(self) -> int:
        return self._rows

    @property
    def first(self) -> _FakeRowLocator:
        return self

    def locator(self, *_args, **_kwargs) -> _FakeRowLocator:
        return self

    def evaluate(self, *_args, **_kwargs) -> str:
        return self._href


class _FakeReservationPage:
    """A page whose Reservation row survives the delete unless *delete_succeeds*.

    Models the shape the production view actually has: a rejected delete is reported as
    a message and still answered with a redirect, so the submit itself always looks fine.
    """

    def __init__(self, delete_succeeds: bool):
        self.delete_succeeds = delete_succeeds
        self.submitted = False
        self.url = "http://netbox.invalid/start"

    def goto(self, url: str) -> None:
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        pass

    def evaluate(self, *_args, **_kwargs) -> None:
        self.submitted = True

    def wait_for_url(self, *_args, **_kwargs) -> None:
        self.url = "http://netbox.invalid/reservations4/"

    def locator(self, *_args, **_kwargs) -> _FakeRowLocator:
        return _FakeRowLocator(0 if (self.submitted and self.delete_succeeds) else 1)


def _run_reservation_cleanup(delete_succeeds: bool) -> list[str]:
    """Run the real cleanup helper against a fake page and return its warnings."""
    pytest.importorskip("playwright", reason="pytest-playwright is a dev dependency")
    spec = importlib.util.spec_from_file_location("_kea_browser_suite", _BROWSER_SUITE / "test_workflows.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    page = _FakeReservationPage(delete_succeeds)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module.TestReservationCRUD()._ui_cleanup_reservation(page, "/plugins/netbox_kea", 1)
    assert page.submitted, "The helper never submitted the delete; this guard would prove nothing."
    return [str(entry.message) for entry in caught]


def test_the_reservation_cleanup_reports_a_delete_the_server_refused():
    """The delete view answers a refused delete with a redirect, not an error status.

    It catches the Kea failure, flashes a message and still redirects, so the submit
    alone cannot tell removal from refusal. Without the re-query the helper stays silent
    and the identifier survives in the shared daemon, blocking the next run.
    """
    assert _run_reservation_cleanup(delete_succeeds=True) == []
    assert _run_reservation_cleanup(delete_succeeds=False), (
        "A Reservation that survived the delete produced no warning."
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
