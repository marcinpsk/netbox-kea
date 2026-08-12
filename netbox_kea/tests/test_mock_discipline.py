# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The mock-discipline guard runs as part of the suite, plus self-tests of the analyzer.

See ``netbox_kea/tests/mock_discipline.py`` for the policy: spec-less MagicMock/Mock used as
object stand-ins are flagged, as is patching our own code without ``autospec=``; bound
(``spec=``/``wraps=``/``autospec=``), inline-``# mock-ok``-marked, or baseline-grandfathered
usages are allowed.
"""

from __future__ import annotations

from netbox_kea.tests import mock_discipline as md
from netbox_kea.tests.mock_discipline import (
    Violation,
    _comment_lines,
    _counts_by_site,
    load_baseline,
    save_baseline,
    scan_source,
    scan_tree,
    unapproved,
)


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


def test_asyncmock_flagged_when_enabled():
    """This plugin is sync-only, so AsyncMock is flagged too (INCLUDE_ASYNCMOCK = True)."""
    src = "from unittest.mock import AsyncMock\n\ndef test_x():\n    return AsyncMock()\n"
    hits = scan_source(src, "t.py")
    assert [h.mock for h in hits] == ["AsyncMock"]


def test_asyncmock_bound_with_spec_is_accepted():
    """A spec-bound AsyncMock (real awaitable interface) is still allowed."""
    src = "from unittest.mock import AsyncMock\nclass C: ...\n\ndef test_x():\n    return AsyncMock(spec=C)\n"
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


# ── unspecced patches of first-party code ────────────────────────────────────────────


_PATCH_IMPORT = "from unittest.mock import patch\n"


def test_flags_unspecced_patch_of_first_party_target():
    """``patch("netbox_kea...")`` yields the same fabricating MagicMock as ``MagicMock()``."""
    src = _PATCH_IMPORT + '\n@patch("netbox_kea.views.leases.fetch_subnet_choices")\ndef test_x(m):\n    pass\n'
    hits = scan_source(src, "t.py")
    assert len(hits) == 1
    assert hits[0].kind == "patch"
    assert hits[0].qualname == "test_x"


def test_accepts_autospecced_patch():
    src = (
        _PATCH_IMPORT
        + '\n@patch("netbox_kea.views.leases.fetch_subnet_choices", autospec=True)\ndef test_x(m):\n    pass\n'
    )
    assert scan_source(src, "t.py") == []


def test_accepts_patch_given_a_ready_made_replacement():
    """``new=``, a non-mock factory, and positional ``new`` avoid the default mock."""
    for call in (
        'patch("netbox_kea.x.y", new=object())',
        'patch("netbox_kea.x.y", new_callable=lambda: object())',
        'patch("netbox_kea.x.y", object())',
    ):
        assert scan_source(f"{_PATCH_IMPORT}\ndef test_x():\n    with {call}:\n        pass\n", "t.py") == [], call


def test_flags_patch_with_unbounded_mock_new_callable():
    """A fabricating mock class still needs a spec when patch uses it as a factory."""
    for mock_class in ("MagicMock", "NonCallableMagicMock", "Mock", "NonCallableMock", "AsyncMock"):
        src = (
            f"from unittest.mock import {mock_class}, patch\n\n"
            "def test_x():\n"
            f'    with patch("netbox_kea.x.y", new_callable={mock_class}):\n'
            "        pass\n"
        )
        assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"], mock_class


def test_accepts_patch_with_spec_bounded_mock_new_callable():
    """A spec passed through patch bounds the mock produced by new_callable."""
    src = (
        "from unittest.mock import MagicMock, patch\n\n"
        "class Thing:\n"
        "    pass\n\n"
        "def test_x():\n"
        '    with patch("netbox_kea.x.y", new_callable=MagicMock, spec=Thing):\n'
        "        pass\n"
    )
    assert scan_source(src, "t.py") == []


def test_flags_patch_with_new_set_to_the_default_sentinel():
    """`new=DEFAULT` is patch()'s "not given" marker, so it builds the same unspecced mock."""
    for call in (
        'patch("netbox_kea.x.y", new=DEFAULT)',
        'patch("netbox_kea.x.y", new=mock.DEFAULT)',
        'patch.object(Server, "get_client", new=DEFAULT)',
    ):
        src = (
            "from unittest.mock import DEFAULT, patch\n"
            "import unittest.mock as mock\n"
            "from netbox_kea.models import Server\n\n"
            f"def test_x():\n    with {call}:\n        pass\n"
        )
        assert [h.kind for h in scan_source(src, "t.py")] == ["patch"], call


def test_flags_positional_default_sentinel_and_renamed_import():
    """Every spelling of unittest.mock.DEFAULT still asks patch() to create its default mock."""
    for call in (
        'patch("netbox_kea.x.y", DEFAULT_SENTINEL)',
        'patch.object(Server, "get_client", DEFAULT_SENTINEL)',
    ):
        src = (
            "from unittest.mock import DEFAULT as DEFAULT_SENTINEL, patch\n"
            "from netbox_kea.models import Server\n\n"
            f"def test_x():\n    with {call}:\n        pass\n"
        )
        assert [h.kind for h in scan_source(src, "t.py")] == ["patch"], call


def test_flags_patch_with_none_spec():
    """An explicit None spec is patch()'s default and does not constrain its generated mock."""
    src = _PATCH_IMPORT + '\ndef test_x():\n    with patch("netbox_kea.x.y", spec=None):\n        pass\n'
    assert [h.kind for h in scan_source(src, "t.py")] == ["patch"]


def test_accepts_unrelated_values_named_default_as_replacements():
    """Only unittest.mock.DEFAULT asks patch() to generate a mock."""
    for replacement in ("DEFAULT", "settings.DEFAULT"):
        src = (
            "from unittest.mock import patch\n\n"
            "DEFAULT = object()\n"
            "class settings:\n"
            "    DEFAULT = object()\n\n"
            f'def test_x():\n    with patch("netbox_kea.x.y", new={replacement}):\n        pass\n'
        )
        assert scan_source(src, "t.py") == [], replacement


def test_accepts_imported_default_shadowed_at_module_scope():
    """A module binding named DEFAULT replaces the imported sentinel."""
    src = (
        "from unittest.mock import DEFAULT, patch\n\n"
        "DEFAULT = object()\n"
        'with patch("netbox_kea.x.y", new=DEFAULT):\n'
        "    pass\n"
    )
    assert scan_source(src, "t.py") == []


def test_accepts_imported_default_shadowed_at_function_scope():
    """A function-local DEFAULT hides the sentinel imported by the module."""
    src = (
        "from unittest.mock import DEFAULT, patch\n\n"
        "def test_x():\n"
        "    DEFAULT = object()\n"
        '    with patch("netbox_kea.x.y", new=DEFAULT):\n'
        "        pass\n"
    )
    assert scan_source(src, "t.py") == []


def test_function_local_default_does_not_shadow_decorator_expression():
    """A decorator resolves DEFAULT before the function-local scope exists."""
    src = (
        "from unittest.mock import DEFAULT, patch\n\n"
        '@patch("netbox_kea.x.y", new=DEFAULT)\n'
        "def test_x():\n"
        "    DEFAULT = object()\n"
    )
    assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"]


def test_function_local_default_does_not_shadow_default_expression():
    """A parameter default resolves DEFAULT in the enclosing scope."""
    src = (
        "from unittest.mock import DEFAULT, patch\n\n"
        'def test_x(value=patch("netbox_kea.x.y", new=DEFAULT)):\n'
        "    DEFAULT = object()\n"
    )
    assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"]


def test_class_binding_does_not_shadow_imported_default_inside_method():
    """A method resolves bare names outside its class namespace."""
    src = (
        "from unittest.mock import DEFAULT, patch\n\n"
        "class TestThing:\n"
        "    DEFAULT = object()\n\n"
        "    def test_x(self):\n"
        '        with patch("netbox_kea.x.y", new=DEFAULT):\n'
        "            pass\n"
    )
    assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"]


def test_flags_default_through_unittest_package_alias():
    """An alias of the unittest package still exposes the real mock.DEFAULT sentinel."""
    src = (
        "import unittest as ut\n"
        "from unittest import mock\n"
        "from unittest.mock import patch\n\n"
        'def test_x():\n    with patch("netbox_kea.x.y", new=ut.mock.DEFAULT):\n        pass\n'
    )
    assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"]


def test_accepts_patch_with_a_real_new_value():
    """A genuine `new=` still binds, so it must not be caught by the DEFAULT check."""
    src = (
        "from unittest.mock import patch\n\n"
        "def replacement():\n    return 1\n\n"
        'def test_x():\n    with patch("netbox_kea.x.y", new=replacement):\n        pass\n'
    )
    assert scan_source(src, "t.py") == []


def test_ignores_patches_of_real_external_boundaries():
    """Stubbing the HTTP boundary is the endorsed pattern, not a discipline violation."""
    src = _PATCH_IMPORT + '\n@patch("requests.Session.post")\ndef test_x(m):\n    pass\n'
    assert scan_source(src, "t.py") == []


def test_flags_patch_object_on_an_imported_first_party_name():
    src = "from unittest.mock import patch\nfrom netbox_kea.models import Server\n\ndef test_x():\n    with patch.object(Server, 'get_client'):\n        pass\n"
    hits = scan_source(src, "t.py")
    assert [h.kind for h in hits] == ["patch"]


def test_flags_patch_object_through_a_relative_import():
    """Test modules import their own package relatively; those targets are ours too."""
    src = "from unittest.mock import patch\nfrom ..models import Server\n\ndef test_x():\n    with patch.object(Server, 'get_client'):\n        pass\n"
    assert [h.kind for h in scan_source(src, "t.py")] == ["patch"]


def test_ignores_unrelated_attribute_patch_calls():
    """An unrelated object named patch is not unittest.mock.patch."""
    src = (
        "import tool\n"
        "from netbox_kea.models import Server\n\n"
        "def test_x():\n"
        '    tool.patch("netbox_kea.x.y")\n'
        '    tool.patch.object(Server, "get_client")\n'
    )
    assert scan_source(src, "t.py") == []


def test_flags_patch_through_mock_module_aliases():
    """Supported spellings of the unittest.mock module remain recognized."""
    cases = (
        ("import unittest.mock as mock\n", "mock.patch"),
        ("import unittest as ut\n", "ut.mock.patch"),
        ("from unittest import mock as m\n", "m.patch"),
    )
    for import_statement, patch_name in cases:
        src = import_statement + f'\ndef test_x():\n    with {patch_name}("netbox_kea.x.y"):\n        pass\n'
        assert [hit.kind for hit in scan_source(src, "t.py")] == ["patch"], patch_name


def test_ignores_direct_patch_alias_rebound_in_function():
    """A function-local binding hides the patch imported by the module."""
    src = (
        "from unittest.mock import patch\n"
        "import tool\n\n"
        "def test_x():\n"
        "    patch = tool.patch\n"
        '    patch("netbox_kea.x.y")\n'
    )
    assert scan_source(src, "t.py") == []


def test_ignores_mock_module_alias_rebound_in_function():
    """A function-local binding hides the mock module imported by the module."""
    src = (
        "import unittest.mock as mock\n"
        "import tool\n\n"
        "def test_x():\n"
        "    mock = tool\n"
        '    mock.patch("netbox_kea.x.y")\n'
    )
    assert scan_source(src, "t.py") == []


def test_limits_local_patch_import_to_its_scope():
    """A direct patch import does not leak into a sibling function."""
    src = (
        "import tool\n\n"
        "def test_first():\n"
        "    from unittest.mock import patch\n"
        '    patch("netbox_kea.x.y")\n\n'
        "def test_second():\n"
        "    patch = tool.patch\n"
        '    patch("netbox_kea.x.y")\n'
    )
    assert [hit.qualname for hit in scan_source(src, "t.py")] == ["test_first"]


def test_limits_local_mock_module_import_to_its_scope():
    """A mock module import does not leak into a sibling function."""
    src = (
        "import tool\n\n"
        "def test_first():\n"
        "    import unittest.mock as mock\n"
        '    mock.patch("netbox_kea.x.y")\n\n'
        "def test_second():\n"
        "    mock = tool\n"
        '    mock.patch("netbox_kea.x.y")\n'
    )
    assert [hit.qualname for hit in scan_source(src, "t.py")] == ["test_first"]


def test_ignores_patch_object_on_a_non_first_party_name():
    src = "from unittest.mock import patch\nimport requests\n\ndef test_x():\n    with patch.object(requests.Session, 'post'):\n        pass\n"
    assert scan_source(src, "t.py") == []


def test_ignores_patch_dict():
    """``patch.dict`` swaps dictionary contents; no mock object is created."""
    src = "from unittest.mock import patch\nfrom netbox_kea import constants\n\ndef test_x():\n    with patch.dict(constants.THING, {}):\n        pass\n"
    assert scan_source(src, "t.py") == []


def test_patch_marker_opt_out_is_honoured():
    src = (
        _PATCH_IMPORT
        + "\n# mock-ok: the real job enqueues to a live queue\n"
        + '@patch("netbox_kea.jobs.KeaIpamSyncJob")\ndef test_x(m):\n    pass\n'
    )
    assert scan_source(src, "t.py") == []


def test_patch_and_mock_violations_share_one_baseline_budget(tmp_path):
    """Both shapes count against the same per-site allowance."""
    src = (
        "from unittest.mock import MagicMock, patch\n\n"
        "def test_x():\n"
        "    row = MagicMock()\n"
        '    with patch("netbox_kea.x.y"):\n'
        "        pass\n"
    )
    hits = scan_source(src, "t.py")
    assert sorted(h.kind for h in hits) == ["mock", "patch"]
    assert _counts_by_site(hits) == {"t.py::test_x": 2}


def test_scan_tree_skips_the_guard_and_its_test():
    """The guard never reports its own files (which mention mock class names)."""
    files = {v.path for v in scan_tree()}
    assert "mock_discipline.py" not in files
    assert "test_mock_discipline.py" not in files


def test_violation_str_format():
    """Violation renders the file:line: message a developer sees."""
    v = Violation("sub/test_x.py", 12, "TestC.test_y", "MagicMock")
    assert str(v) == "sub/test_x.py:12: unapproved MagicMock() in TestC.test_y()"
    assert v.site == "sub/test_x.py::TestC.test_y"


def test_patch_violation_str_names_the_target():
    v = Violation("sub/test_x.py", 12, "TestC.test_y", "'netbox_kea.x.y'", kind="patch")
    assert str(v) == "sub/test_x.py:12: unspecced patch of first-party 'netbox_kea.x.y' in TestC.test_y()"


def test_comment_lines_handles_unparsable_source():
    """A tokenizer error (e.g. an unterminated string) degrades to {} rather than raising."""
    assert _comment_lines("x = 'unterminated\n") == {}


def test_load_baseline_missing_file_returns_empty(tmp_path):
    assert load_baseline(tmp_path / "nope.txt") == {}


def test_save_and_load_baseline_roundtrip(tmp_path):
    """save_baseline writes a header + sorted entries that load_baseline reads back exactly."""
    counts = {"b.py::g": 1, "a.py::f": 2}
    path = tmp_path / "baseline.txt"
    save_baseline(counts, path)
    text = path.read_text()
    assert "Mock-discipline baseline" in text  # explanatory header written (incl. SPDX tags)
    assert not text.endswith("\n\n")  # exactly one trailing newline
    assert load_baseline(path) == counts


def test_main_update_baseline_writes_and_reports(capsys):
    """`--update-baseline` rescans, rewrites the baseline, and reports the count (exit 0).

    The real baseline is currently empty and the tree is clean, so this regenerates it
    byte-identically; snapshot/restore guards against any future non-empty state.
    """
    backup = md._BASELINE_PATH.read_text()
    try:
        rc = md._main(["--update-baseline"])
    finally:
        md._BASELINE_PATH.write_text(backup)
    assert rc == 0
    assert "baseline updated" in capsys.readouterr().out


def test_main_reports_and_exits_nonzero_on_violation(capsys):
    """`_main([])` prints each unapproved mock and returns 1 when the tree has one."""
    bad_file = md.TESTS_ROOT / "_tmp_mockcheck_cov.py"
    bad_file.write_text("from unittest.mock import MagicMock\n\ndef test_x():\n    return MagicMock()\n")
    try:
        rc = md._main([])
        out = capsys.readouterr().out
    finally:
        bad_file.unlink()
    assert rc == 1
    assert "_tmp_mockcheck_cov.py" in out
    assert "unapproved mock" in out


def test_main_clean_tree_exits_zero(capsys):
    """`_main([])` returns 0 and reports zero when the tree is clean (current state)."""
    rc = md._main([])
    assert rc == 0
    assert "0 unapproved mock(s)" in capsys.readouterr().out
