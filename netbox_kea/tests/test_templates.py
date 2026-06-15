# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Static correctness checks on the plugin's Django templates."""

from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

import netbox_kea

_TEMPLATES_DIR = Path(netbox_kea.__file__).parent / "templates"


def _multiline_comment_lines(text: str) -> list[int]:
    """Return the 1-based line numbers that open a ``{#`` comment left unclosed on that line.

    Django tokenises comments with ``{#.*?#}`` and *no* ``re.DOTALL`` flag, so a
    ``{#`` whose matching ``#}`` is on a later line is **not** recognised as a
    comment — the literal text (including the ``{#``) renders into the page. This
    detects that mistake without needing to render anything.
    """
    offenders: list[int] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        idx = line.find("{#")
        if idx != -1 and "#}" not in line[idx:]:
            offenders.append(lineno)
    return offenders


class TestTemplateComments(SimpleTestCase):
    """Guard against multi-line ``{# #}`` comments that leak as visible text.

    Regression: multi-line ``{# … #}`` comments rendered literally on the lease
    search form and reservation Add form because Django only recognises
    single-line template comments.
    """

    def test_detector_flags_a_multiline_comment(self):
        """The detector itself must fire on a known-bad comment (so the scan isn't vacuous)."""
        bad = "<div>\n{# this comment\n   spans two lines #}\n</div>\n"
        self.assertEqual(_multiline_comment_lines(bad), [2])

    def test_detector_accepts_single_line_comments(self):
        """A well-formed single-line comment (and a normal line) must not be flagged."""
        good = '{# a header comment #}\n<input id="id_q">\n{# another note #}\n'
        self.assertEqual(_multiline_comment_lines(good), [])

    def test_no_multiline_comments_in_plugin_templates(self):
        """No shipped template may contain a multi-line ``{# #}`` comment."""
        offenders: list[str] = []
        templates = sorted(_TEMPLATES_DIR.rglob("*.html"))
        self.assertTrue(templates, f"No templates found under {_TEMPLATES_DIR}")
        for path in templates:
            offenders.extend(
                f"{path.relative_to(_TEMPLATES_DIR)}:{lineno}"
                for lineno in _multiline_comment_lines(path.read_text(encoding="utf-8"))
            )
        self.assertEqual(
            offenders,
            [],
            "Multi-line {# #} Django comments leak as literal text — use "
            "{% comment %}…{% endcomment %} instead:\n  " + "\n  ".join(offenders),
        )
