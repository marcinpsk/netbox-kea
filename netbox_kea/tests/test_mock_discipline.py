# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The mock-discipline guard runs as part of the suite, plus self-tests of the analyzer.

See ``netbox_kea/tests/mock_discipline.py`` for the policy: spec-less MagicMock/Mock used as
object stand-ins are flagged; bound (``spec=``/``wraps=``), inline-``# mock-ok``-marked, or
baseline-grandfathered usages are allowed.
"""

from __future__ import annotations

from netbox_kea.tests.mock_discipline import _counts_by_site, scan_source, scan_tree, unapproved


def test_no_unapproved_mocks_beyond_baseline():
    """No new spec-less MagicMock/Mock has crept in past the grandfathered baseline.

    To resolve a failure, prefer (in order): use a real object, bound the mock with
    ``spec=`` / ``wraps=``, or add an inline ``# mock-ok: <reason>``. Only as a last
    resort regenerate the baseline: ``python3 netbox_kea/tests/mock_discipline.py --update-baseline``.
    """
    bad = unapproved()
    assert not bad, (
        "Unapproved attribute-fabricating mock(s):\n"
        + "\n".join(f"  {v}" for v in bad)
        + (
            "\n\nFix by: using a real object, binding with spec=/wraps=, or marking the line "
            "`# mock-ok: <reason>`. Last resort: python3 netbox_kea/tests/mock_discipline.py --update-baseline"
        )
    )


# ── analyzer self-tests (real AST parsing — no mocks of the thing that hunts mocks) ──


def test_flags_specless_magicmock():
    src = "from unittest.mock import MagicMock\n\ndef test_x():\n    row = MagicMock()\n"
    hits = scan_source(src, "t.py")
    assert len(hits) == 1
    assert hits[0].mock == "MagicMock"
    assert hits[0].qualname == "test_x"
    assert hits[0].site == "t.py::test_x"


def test_flags_bare_mock_and_aliased_import():
    src = "from unittest.mock import Mock as M\n\ndef test_x():\n    return M()\n"
    hits = scan_source(src, "t.py")
    assert [h.mock for h in hits] == ["Mock"]


def test_flags_attribute_access_form():
    src = "import unittest.mock as m\n\ndef test_x():\n    return m.MagicMock()\n"
    hits = scan_source(src, "t.py")
    assert [h.mock for h in hits] == ["MagicMock"]


def test_accepts_spec_bounded_mock():
    src = "from unittest.mock import MagicMock\nclass C: ...\n\ndef test_x():\n    return MagicMock(spec=C)\n"
    assert scan_source(src, "t.py") == []


def test_accepts_wraps_and_spec_set():
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x(real):\n"
        "    a = MagicMock(wraps=real)\n"
        "    b = MagicMock(spec_set=real)\n"
        "    return a, b\n"
    )
    assert scan_source(src, "t.py") == []


def test_accepts_inline_marker():
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    client = MagicMock()  # mock-ok: external Kea HTTP boundary\n"
        "    return client\n"
    )
    assert scan_source(src, "t.py") == []


def test_marker_must_be_in_a_comment_not_a_string():
    """A `mock-ok` inside a string literal does not count as an opt-out marker."""
    src = 'from unittest.mock import MagicMock\n\ndef test_x():\n    label = "mock-ok"\n    return MagicMock()\n'
    hits = scan_source(src, "t.py")
    assert len(hits) == 1


def test_asyncmock_not_flagged_by_default():
    src = "from unittest.mock import AsyncMock\n\ndef test_x():\n    return AsyncMock()\n"
    assert scan_source(src, "t.py") == []


def test_marker_in_comment_block_above_statement_is_honoured():
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    # mock-ok: external boundary\n"
        "    # (second line of the reason)\n"
        "    client = MagicMock()\n"
        "    return client\n"
    )
    assert scan_source(src, "t.py") == []


def test_marker_above_does_not_leak_across_a_blank_line():
    """A marker comment separated from the mock by a blank line does NOT silence it."""
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    # mock-ok: this belongs to something else\n"
        "\n"
        "    return MagicMock()\n"
    )
    assert len(scan_source(src, "t.py")) == 1


def test_marker_on_multiline_call_is_honoured():
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    return MagicMock(  # mock-ok: boundary\n"
        "        return_value=1\n"
        "    )\n"
    )
    assert scan_source(src, "t.py") == []


def test_counts_by_site_groups_per_function():
    src = (
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    a = MagicMock()\n"
        "    b = MagicMock()\n"
        "    return a, b\n"
    )
    counts = _counts_by_site(scan_source(src, "t.py"))
    assert counts == {"t.py::test_x": 2}


def test_baseline_budget_allows_grandfathered_but_not_excess(tmp_path):
    """A site with N grandfathered mocks tolerates N but flags the N+1-th."""
    pkg = tmp_path / "tests"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "test_thing.py").write_text(
        "from unittest.mock import MagicMock\n\n"
        "def test_x():\n"
        "    a = MagicMock()\n"
        "    b = MagicMock()\n"
        "    return a, b\n"
    )
    # Budget of 1 for the two-mock site → exactly one excess is reported.
    extra = unapproved(root=pkg, baseline={"test_thing.py::test_x": 1})
    assert len(extra) == 1
    # Budget of 2 → nothing reported.
    assert unapproved(root=pkg, baseline={"test_thing.py::test_x": 2}) == []


def test_scan_tree_skips_the_guard_and_its_test():
    """The guard never reports its own files (which mention mock class names)."""
    files = {v.path for v in scan_tree()}
    assert "mock_discipline.py" not in files
    assert "test_mock_discipline.py" not in files
