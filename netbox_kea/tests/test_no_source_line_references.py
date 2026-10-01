# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""No comment or docstring cites a source line number.

A line number is wrong after the next edit above it, so a reference like
"Lines 731-736" soon points at unrelated code. Name the function or the
behaviour instead.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCANNED_TREES = ("netbox_kea", "tests", "scripts")
# A one-digit number is a line of inline test data, never a cited source line.
SOURCE_LINE_REFERENCE = re.compile(r"\b[Ll]ines?\s+~?\d{2,}\b|\bL\d{3,}\b|\b[\w./-]+\.py:\d{2,}\b")


def _references(text: str, name: str) -> list[str]:
    """Return ``name:line: text`` for each source-line reference in a comment or docstring of *text*."""
    lines = text.splitlines()
    found: set[int] = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT and SOURCE_LINE_REFERENCE.search(token.string)
    }
    # A bare string statement is a docstring, or documents the attribute above it.
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            found.update(number for number in span if SOURCE_LINE_REFERENCE.search(lines[number - 1]))
    return [f"{name}:{number}: {lines[number - 1].strip()}" for number in sorted(found)]


def _scanned_files() -> list[Path]:
    """Return the Python files of the scanned trees, without this file (it holds the samples)."""
    this_file = Path(__file__).resolve()
    return sorted(
        path for tree in SCANNED_TREES for path in (REPOSITORY_ROOT / tree).rglob("*.py") if path.resolve() != this_file
    )


@pytest.mark.parametrize(
    "text",
    [
        '"""Lines 731-736: exception during HTMX partial rendering."""',
        '"""Line 931: lease edit POST with an invalid form."""',
        '"""v6 lease GET includes duid in form initial (line 910)."""',
        "# get_export: invalid form (lines 563-564)",
        "# ── 1. config-get returns non-dict arguments (~lines 92-99) ──",
        "# and test the explicit guard at line 944-945.",
        "# see L1234 in kea.py",
        "# see kea.py:123",
        "# see netbox_kea/views/subnets.py:1042-1050",
    ],
)
def test_the_pattern_matches_a_source_line_reference(text: str) -> None:
    assert SOURCE_LINE_REFERENCE.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "\"f'reservation-{operation}' (line 1)\",",
        '["chosen_command (line 1)", "f\'{prefix}-get\' (line 2)"],',
        '"netbox_kea/jobs.py:1: error: Nonexistent baseline finding  [arg-type]"',
        "max_lines 200",
        "the first 10 lines of the file",
        '"""F10: _enrich_lease() must inject expiry_class."""',
        "VLAN100 and L2 segments",
    ],
)
def test_the_pattern_ignores_text_that_is_not_a_source_line_reference(text: str) -> None:
    assert not SOURCE_LINE_REFERENCE.search(text), text


def test_the_scan_reports_the_file_and_line_of_each_reference() -> None:
    text = "x = 1\n# see lines 10-12\ny = 2  # (line 40)\n"

    assert _references(text, "a.py") == ["a.py:2: # see lines 10-12", "a.py:3: y = 2  # (line 40)"]


def test_the_scan_reports_each_line_of_a_docstring() -> None:
    text = 'def f():\n    """Do it.\n\n    See line 931.\n    """\n'

    assert _references(text, "a.py") == ["a.py:4: See line 931."]


def test_the_scan_ignores_a_reference_in_an_ordinary_string() -> None:
    text = (
        'message = "Error at line 100"\n'
        'assert str(v) == "sub/test_x.py:12: unapproved MagicMock()"  # the reported location\n'
    )

    assert _references(text, "a.py") == []


def test_the_scan_covers_the_production_code_and_both_test_suites() -> None:
    names = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in _scanned_files()}

    assert {"netbox_kea/kea.py", "netbox_kea/tests/conftest.py", "tests/conftest.py"} <= names


def test_no_comment_or_docstring_cites_a_source_line_number() -> None:
    found = [
        reference
        for path in _scanned_files()
        for reference in _references(path.read_text(encoding="utf-8"), path.relative_to(REPOSITORY_ROOT).as_posix())
    ]

    assert not found, (
        "Name the function or the behaviour, not a source line number, "
        "because line numbers change on each edit:\n" + "\n".join(found)
    )
