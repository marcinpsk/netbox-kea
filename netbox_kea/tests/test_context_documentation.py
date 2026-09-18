# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the repository's domain-language documentation."""

import os
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_context_defines_the_dhcp_option_term_consistently():
    """Keep the Catalogue glossary on its canonical singular domain term."""
    context = (REPOSITORY_ROOT / "CONTEXT.md").read_text()

    assert context.count("**DHCP Option**:") == 1
    assert "DHCP Options" not in context


#: Dot-directories that do hold documentation this repository publishes. Every other
#: one is tooling state or, like ``.claude`` and ``.handoff``, a local working file.
_PUBLISHED_DOT_DIRECTORIES = {".github", ".opengrep"}
_SKIPPED_DIRECTORIES = {"node_modules", "__pycache__"}
#: A Markdown link: ``[text](target)``. The target may carry a title or angle brackets.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^)]*)?\)")
#: A fenced code block, whose sample links are not links this repository publishes.
_CODE_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)


def _repository_markdown() -> list[Path]:
    """Return every Markdown file this repository publishes.

    ``.gitignore`` keeps ``.claude/`` and ``.handoff/`` out of the repository, and
    ``.claude/`` holds working copies of the whole tree, so a plain glob from the root
    reads every one of them. A nested checkout carries its own ``.git``, which is the
    other thing pruned here. ``git ls-files`` would say this exactly, but it exits 128
    in the devcontainer this suite runs in, where the linked worktree's gitdir points
    at a host path.
    """
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(REPOSITORY_ROOT):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _SKIPPED_DIRECTORIES
            and (not name.startswith(".") or name in _PUBLISHED_DOT_DIRECTORIES)
            and not Path(directory, name, ".git").exists()
        ]
        found.extend(Path(directory, name) for name in files if name.endswith(".md"))
    return sorted(found)


def _relative_link_targets(document: Path):
    """Yield every link in *document* that names a path in this repository."""
    body = _CODE_FENCE.sub("", document.read_text(encoding="utf-8"))
    for target in _MARKDOWN_LINK.findall(body):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target


def _resolved_link(document: Path, target: str) -> Path:
    """Resolve *target* the way a reader following the link on GitHub would."""
    path = target.split("#", maxsplit=1)[0]
    base = REPOSITORY_ROOT if path.startswith("/") else document.parent
    return (base / path.lstrip("/")).resolve()


def _links_that_do_not_resolve(document: Path) -> list[str]:
    """Return every link in *document* that leaves the repository or names nothing."""
    broken = []
    for target in _relative_link_targets(document):
        resolved = _resolved_link(document, target)
        if not resolved.is_relative_to(REPOSITORY_ROOT) or not resolved.exists():
            broken.append(target)
    return broken


def test_every_relative_documentation_link_resolves():
    """A `../../` link from `docs/` escapes the repository and 404s on GitHub.

    The brief reached one directory too far on eight links, which renders as a dead
    link rather than as an error anywhere, so nothing but a reader would notice.
    """
    documents = _repository_markdown()
    assert documents, "no Markdown was found; this guard has stopped reading the repository."

    broken = [
        f"{document.relative_to(REPOSITORY_ROOT)}: {target}"
        for document in documents
        for target in _links_that_do_not_resolve(document)
    ]

    assert not broken, f"these documentation links do not resolve inside the repository: {broken}."


def test_the_guard_rejects_a_link_that_leaves_the_repository(tmp_path):
    """Existence alone would pass a link that climbs out of the checkout.

    This repository keeps its worktrees one directory below the root, so a link that
    escapes can resolve on the machine running the suite and still 404 on GitHub.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("stand-in for a file beside the checkout\n", encoding="utf-8")
    document = REPOSITORY_ROOT / "docs" / "example.md"
    escaping = f"{'../' * len(REPOSITORY_ROOT.parts)}{outside.relative_to(outside.anchor)}"

    assert _resolved_link(document, escaping) == outside
    assert outside.exists()
    assert not _resolved_link(document, escaping).is_relative_to(REPOSITORY_ROOT)


def test_the_guard_reads_only_the_links_a_reader_can_follow(tmp_path):
    """A title, angle brackets and an absolute target all name one file. A fence is not a link."""
    document = tmp_path / "example.md"
    document.write_text(
        "[plain](../README.md)\n"
        '[titled](../README.md "The readme")\n'
        "[angled](<../README.md>)\n"
        "[absolute](/README.md)\n"
        "```\n[fenced example](../../nowhere.md)\n```\n",
        encoding="utf-8",
    )

    targets = list(_relative_link_targets(document))

    assert targets == ["../README.md", "../README.md", "../README.md", "/README.md"]
    assert [_resolved_link(REPOSITORY_ROOT / "docs" / "x.md", target) for target in targets] == [
        REPOSITORY_ROOT / "README.md"
    ] * 4
