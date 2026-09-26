# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""CHANGELOG.md must carry the marker python-semantic-release writes into.

In "update" mode the release job splits the existing changelog on an insertion
flag and puts the new section between the halves. Its own template says what
happens when the flag is absent from a file that has content: "file will not be
updated". It re-emits the file unchanged, logs nothing and exits 0, so the
release still tags, builds, publishes to PyPI and writes GitHub release notes
from a separate template. Ten releases landed that way with CHANGELOG.md
untouched.

Nothing in the release job can fail on this, so the guard has to live here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 needs the dev group's explicit TOML reader.
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def changelog_config() -> dict:
    """Return the changelog block of the semantic-release configuration."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["semantic_release"]
    return config["changelog"]


def test_changelog_settings_are_stated_not_inherited(changelog_config: dict) -> None:
    """Read both keys from the file, so this suite asserts no default of its own."""
    assert "mode" in changelog_config, "state changelog.mode; the guard below reads it"
    assert "insertion_flag" in changelog_config, "state changelog.insertion_flag; the guard below reads it"


def test_the_configured_changelog_carries_the_insertion_flag(changelog_config: dict) -> None:
    """Update mode writes at the flag and silently writes nowhere without it."""
    if changelog_config["mode"] != "update":
        pytest.skip("only update mode splits an existing file on the insertion flag")

    changelog = REPOSITORY_ROOT / changelog_config["default_templates"]["changelog_file"]
    assert changelog.is_file(), f"{changelog} is missing"

    flag = changelog_config["insertion_flag"]
    assert flag in changelog.read_text(encoding="utf-8"), (
        f"{changelog.name} has no {flag!r}, so semantic-release rewrites it unchanged and every release note is lost."
    )
