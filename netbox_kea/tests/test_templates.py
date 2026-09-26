# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Static correctness checks on the plugin's Django templates."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase

import netbox_kea
from netbox_kea.models import Server

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
        # Walk every ``{#`` on the line — inspecting only the first occurrence
        # would miss a later unclosed marker like ``{# ok #} {# broken``.
        pos = 0
        while True:
            idx = line.find("{#", pos)
            if idx == -1:
                break
            end = line.find("#}", idx + 2)
            if end == -1:
                offenders.append(lineno)
                break
            pos = end + 2
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

    def test_detector_flags_later_unclosed_comment_on_same_line(self):
        """A line that closes one comment then opens a second, unclosed ``{#`` is flagged.

        Inspecting only the first ``{#`` per line would miss this — the first
        comment is closed, so the scan must keep walking past it.
        """
        self.assertEqual(_multiline_comment_lines("{# ok #} {# broken\nmore #}\n"), [1])

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


# ─────────────────────────────────────────────────────────────────────────────
# Model attributes the templates name
# ─────────────────────────────────────────────────────────────────────────────

_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_ATTRIBUTE = re.compile(r"\b(object|server)\.([A-Za-z_][A-Za-z0-9_]*)")

# `object` is NetBox's generic context name, so it is not always a Server. Name every
# template whose view renders something else; everything else renders a Server.
_OBJECT_IS_NOT_A_SERVER = {"netbox_kea/ip_kea_reservations.html": "ipam.IPAddress"}


def _attributes_in_template_source(text: str) -> list[tuple[str, str]]:
    """Return every ``(context name, attribute)`` pair the template tags in *text* read."""
    return [pair for tag in _TAG.findall(text) for pair in _ATTRIBUTE.findall(_QUOTED.sub(" ", tag))]


def _model_attributes_named_by_templates() -> dict[tuple[str, str], set[str]]:
    """Map each ``(model label, attribute)`` the templates read to the templates reading it.

    Every tag, not only ``{{ }}`` and ``{% if %}``: ``{% checkmark object.dhcp4 %}`` and
    ``{% url ... object.pk %}`` read the object just as hard, and a guard that cannot see
    them lets exactly the rename it exists to catch go quiet. Quoted text is stripped
    first, because ``{% include "netbox_kea/server.html" %}`` is a path, not an attribute.
    """
    named: dict[tuple[str, str], set[str]] = {}
    for template in sorted(_TEMPLATES_DIR.rglob("*.html")):
        relative = template.relative_to(_TEMPLATES_DIR).as_posix()
        other = _OBJECT_IS_NOT_A_SERVER.get(relative)
        for name, attribute in _attributes_in_template_source(template.read_text(encoding="utf-8")):
            label = other if name == "object" and other else "netbox_kea.Server"
            named.setdefault((label, attribute), set()).add(relative)
    return named


class TestTemplatesNameRealModelAttributes(SimpleTestCase):
    """A template that reads a renamed field renders blank and raises nothing.

    Migration 0009 renamed server_url to ca_url. Three places kept the old name for two
    releases: the Server panel, the combined overview, and the devcontainer seed script.
    Django resolves a missing attribute to the empty string, so the URL row just went
    blank and nothing failed.
    """

    def test_every_model_attribute_a_template_reads_exists(self):
        named = _model_attributes_named_by_templates()
        missing = sorted(
            f"{label}.{attribute} (in {', '.join(sorted(templates))})"
            for (label, attribute), templates in named.items()
            if not hasattr(apps.get_model(label), attribute)
        )

        self.assertEqual(
            missing,
            [],
            "These templates read a model attribute that does not exist, so Django renders "
            f"an empty string there instead of raising: {missing}",
        )

    def test_the_scan_sees_an_attribute_no_output_tag_holds(self):
        """An earlier version read only `{{ }}` and `{% if %}`, and missed all three of these.

        Asserting against the shipped templates cannot pin this: every attribute they read
        through another tag is also read through a `{{ }}` somewhere, so the narrow scan
        produced the same set and a revert would stay green.
        """
        source = (
            "<td>{% checkmark object.only_in_checkmark %}</td>\n"
            "<form action=\"{% url 'plugins:netbox_kea:server' object.only_in_url %}\"></form>\n"
            "{% with flag=server.only_in_with %}{% endwith %}\n"
        )

        self.assertEqual(
            _attributes_in_template_source(source),
            [("object", "only_in_checkmark"), ("object", "only_in_url"), ("server", "only_in_with")],
        )

    def test_the_scan_reads_no_attribute_out_of_a_quoted_path(self):
        """`{% include "netbox_kea/server.html" %}` is a path; `server.html` is not an attribute."""
        source = "{% include \"netbox_kea/server.html\" %}{% extends 'generic/object.html' %}"

        self.assertEqual(_attributes_in_template_source(source), [])

    def test_the_scan_reads_the_real_templates(self):
        """An empty scan would make the guard above pass without checking anything."""
        self.assertIn(("netbox_kea.Server", "ca_url"), _model_attributes_named_by_templates())

    def test_the_scan_reads_every_attribute_in_one_tag(self):
        """`{% if object.a and object.b %}` names two attributes, not one."""
        source = "{% if object.sync_enabled and object.sync_leases_enabled %}"

        self.assertEqual(
            _attributes_in_template_source(source),
            [("object", "sync_enabled"), ("object", "sync_leases_enabled")],
        )

    def test_the_non_server_template_map_is_current(self):
        """A stale entry would silently check a template against the wrong model."""
        for template, label in _OBJECT_IS_NOT_A_SERVER.items():
            with self.subTest(template):
                self.assertTrue((_TEMPLATES_DIR / template).is_file(), template)
                self.assertIsNotNone(apps.get_model(label))


# ─────────────────────────────────────────────────────────────────────────────
# Server fields the devcontainer seed script names
# ─────────────────────────────────────────────────────────────────────────────

_SEED_SCRIPT = Path(netbox_kea.__file__).parent.parent / ".devcontainer" / "scripts" / "load-sample-data.py"


def _seed_script_server_kwargs() -> set[str]:
    """Return every keyword the seed script passes to ``Server(**kwargs)``."""
    tree = ast.parse(_SEED_SCRIPT.read_text(encoding="utf-8"))
    return {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict"
        for keyword in node.keywords
        if keyword.arg is not None
    }


class TestSeedScriptNamesRealServerFields(SimpleTestCase):
    """`update_or_create(defaults=...)` raises TypeError on a renamed field.

    The same 0009 rename left server_url, username and password in this script. Nothing
    imports it, so no other gate reaches it; this reads its source instead.
    """

    def test_every_field_the_seed_script_sets_exists(self):
        fields = {field.name for field in Server._meta.get_fields()}
        unknown = sorted(_seed_script_server_kwargs() - fields)

        self.assertEqual(
            unknown,
            [],
            f"The devcontainer seed script passes Server kwargs that are not fields: {unknown}",
        )

    def test_the_seed_scan_reads_a_real_script(self):
        """No kwargs found would make the test above pass without checking anything."""
        self.assertIn("ca_url", _seed_script_server_kwargs())
