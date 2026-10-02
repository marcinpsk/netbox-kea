# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The real release generator must preserve every commit's operator guidance."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git import Commit, Repo
from semantic_release.changelog.context import ChangelogMode, make_changelog_context
from semantic_release.changelog.release_history import ReleaseHistory
from semantic_release.changelog.template import environment
from semantic_release.cli.changelog_writer import apply_user_changelog_template_directory, render_default_changelog_file
from semantic_release.cli.config import ChangelogOutputFormat
from semantic_release.commit_parser import ParseError
from semantic_release.commit_parser.conventional import ConventionalCommitParser
from semantic_release.hvcs.github import Github
from semantic_release.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def changelog_config():
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())["tool"]["semantic_release"]["changelog"]


def _render_changelog(tmp_path, config, messages, *, unreleased=False, native=False):
    """Run the installed parser and renderer with the repository's template configuration."""
    parser = ConventionalCommitParser()
    elements = {}
    with Repo(REPOSITORY_ROOT) as repo:
        for index, message in enumerate(messages, start=1):
            commit = parser.parse_commit(Commit(repo, bytes([index]) * 20, message=message))
            category = "unknown" if isinstance(commit, ParseError) else commit.type
            elements.setdefault(category, []).append(commit)
    version = Version.parse("99.0.0")
    history = ReleaseHistory(
        unreleased=elements if unreleased else {},
        released={}
        if unreleased
        else {
            version: {
                "version": version,
                "tagged_date": datetime(2026, 10, 2, tzinfo=timezone.utc),
                "elements": elements,
            },
        },
    )
    previous = tmp_path / "CHANGELOG.md"
    previous.write_text(f"# CHANGELOG\n\n{config['insertion_flag']}\n\n## v1.0.0\n\nExisting release.\n")
    context = make_changelog_context(
        hvcs_client=Github("https://example.invalid/example/project.git", hvcs_domain="https://example.invalid"),
        release_history=history,
        mode=ChangelogMode.UPDATE,
        prev_changelog_file=previous,
        insertion_flag=config["insertion_flag"],
        mask_initial_release=False,
    )
    template_dir = REPOSITORY_ROOT / config.get("template_dir", "templates")
    if native or not template_dir.is_dir():
        return render_default_changelog_file(ChangelogOutputFormat.MARKDOWN, context, "conventional") + "\n"
    template_environment = context.bind_to_environment(
        environment(template_dir=template_dir, **config.get("environment", {}))
    )
    apply_user_changelog_template_directory(template_dir, template_environment, tmp_path)
    return previous.read_text()


@pytest.mark.parametrize("unreleased", [False, True])
def test_every_commit_contributes_release_notices_and_breaking_changes(tmp_path, changelog_config, unreleased):
    """A later ordinary commit must not hide an earlier commit's release guidance."""
    messages = (
        "refactor: simplify reporting",
        (
            "refactor: preserve owner links\n\nNOTICE: Keep the migration receipt until every owner completes.\n\n"
            "BREAKING CHANGE: Blank descriptions require an explicit claim."
        ),
        'refactor: protect legacy rows\n\nNOTICE: Legacy "stale" rows & <markers> stay unowned and counted.',
    )
    rendered = _render_changelog(tmp_path, changelog_config, messages, unreleased=unreleased)
    for text in (
        "Simplify reporting",
        "Preserve owner links",
        "Protect legacy rows",
        "Keep the migration receipt until every owner completes.",
        'Legacy "stale" rows & <markers> stay unowned and counted.',
        "Blank descriptions require an explicit claim.",
        "Existing release.",
    ):
        assert text in rendered, (text, rendered)
    assert rendered.count("### Refactoring") == 1
    assert rendered.count("### Additional Release Information") == 1
    assert rendered.count("### Breaking Changes") == 1
    assert ("## Unreleased" if unreleased else "## v99.0.0 (2026-10-02)") in rendered


@pytest.mark.parametrize("unreleased", [False, True])
def test_ordinary_changelog_format_matches_the_native_renderer(tmp_path, changelog_config, unreleased):
    """Keep native category order, scopes, links, wrapping and existing release content."""
    messages = (
        "refactor(ipam): retain owner confirmation when a sibling snapshot cannot verify every configured subnet identity",
        'refactor: preserve "owner" links & <markers> (#12)',
        "fix(sync): accept complete empty observations",
        "feat: report adoption progress",
        "unstructured history entry",
    )
    expected = _render_changelog(tmp_path, changelog_config, messages, unreleased=unreleased, native=True)
    actual = _render_changelog(tmp_path, changelog_config, messages, unreleased=unreleased)
    assert actual == expected
    assert "unstructured history entry" not in actual
    assert "### Additional Release Information" not in actual
    assert "### Breaking Changes" not in actual
