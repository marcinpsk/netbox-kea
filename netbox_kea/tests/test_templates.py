# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Static correctness checks on the plugin's Django templates."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.template import Engine
from django.template.base import TextNode
from django.template.loader_tags import BlockNode, ExtendsNode
from django.template.utils import get_app_template_dirs
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


# ─────────────────────────────────────────────────────────────────────────────
# Blocks the plugin templates override
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK = re.compile(r"\{%-?\s*block\s+([\w-]+)")
_EXTENDS = re.compile(r"\{%-?\s*extends\s+[\"']([^\"']+)[\"']")
# NetBox renders these only for a view with bulk actions (netbox-community/netbox#23240).
_CONDITIONAL_BULK_BLOCKS = {"bulk_controls", "bulk_buttons", "bulk_extra_controls"}


def _plugin_templates() -> dict[str, str]:
    return {
        path.relative_to(_TEMPLATES_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_TEMPLATES_DIR.rglob("*.html"))
    }


def _top_level_blocks(name: str) -> list[BlockNode]:
    """Return the blocks *name* overrides in its parent, or nothing when it extends no template."""
    extends = next(
        (node for node in Engine.get_default().get_template(name).nodelist if isinstance(node, ExtendsNode)), None
    )
    return [] if extends is None else [node for node in extends.nodelist if isinstance(node, BlockNode)]


def _blocks_netbox_defines() -> set[str]:
    """Every block a NetBox template, or a plugin template another plugin template extends, defines."""
    netbox = Path(settings.BASE_DIR).resolve()
    dirs = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]] + [Path(d) for d in get_app_template_dirs("templates")]
    defined = {
        block
        for directory in dirs
        if directory.resolve().is_relative_to(netbox)
        for path in directory.rglob("*.html")
        for block in _BLOCK.findall(path.read_text(encoding="utf-8"))
    }
    plugin = _plugin_templates()
    parents = {parent for text in plugin.values() for parent in _EXTENDS.findall(text)}
    defined.update(block for name in parents if name in plugin for block in _BLOCK.findall(plugin[name]))
    return defined


class TestOverriddenBlocksRender(SimpleTestCase):
    """A page control in a block that NetBox does not render is lost without an error."""

    def test_every_overridden_block_exists_in_netbox(self):
        defined = _blocks_netbox_defines()
        unknown = sorted(
            f"{name}: {node.name}"
            for name in _plugin_templates()
            for node in _top_level_blocks(name)
            if node.name not in defined
        )
        self.assertEqual(unknown, [], "These blocks exist in no parent template, so Django never renders them")

    def test_no_template_puts_controls_in_a_bulk_action_block(self):
        filled = sorted(
            f"{name}: {node.name}"
            for name in _plugin_templates()
            for node in _top_level_blocks(name)
            if node.name in _CONDITIONAL_BULK_BLOCKS
            and any(not isinstance(child, TextNode) or child.s.strip() for child in node.nodelist)
        )
        self.assertEqual(filled, [], "NetBox drops these blocks on a view without bulk actions")

    def test_the_block_scan_reads_netbox_templates(self):
        """No NetBox template found would make every override look unknown, or the scan vacuous."""
        self.assertTrue({"content", "head", "modals", "bulk_controls"} <= _blocks_netbox_defines())


def _card_header_icons_without_gap(text: str) -> list[str]:
    """Return the class of each card header icon that text follows directly, without a margin class."""
    from bs4 import BeautifulSoup, NavigableString

    offenders = []
    for header in BeautifulSoup(text, "html.parser").select(".card-header"):
        for icon in header.find_all("i", class_="mdi", recursive=False):
            following = icon.next_sibling
            if (
                isinstance(following, NavigableString)
                and following.strip()
                and not any(name.startswith("me-") for name in icon["class"])
            ):
                offenders.append(" ".join(icon["class"]))
    return offenders


class TestCardHeaderIconGap(SimpleTestCase):
    """NetBox draws .card-header as a flex box, which drops the space between an icon and the text after it."""

    def test_detector_flags_an_icon_directly_before_header_text(self):
        bad = '<h5 class="card-header">\n  <i class="mdi mdi-plus"></i>\n  Add {{ thing }}\n</h5>'
        self.assertEqual(_card_header_icons_without_gap(bad), ["mdi mdi-plus"])

    def test_detector_accepts_a_margin_class_or_an_inline_wrapper(self):
        good = (
            '<h5 class="card-header"><i class="mdi mdi-plus me-1"></i> Add</h5>'
            '<div class="card-header"><strong><i class="mdi mdi-magnify"></i> Search</strong></div>'
        )
        self.assertEqual(_card_header_icons_without_gap(good), [])

    def test_every_card_header_icon_has_a_gap(self):
        offenders = sorted(
            f"{name}: {icon}"
            for name, text in _plugin_templates().items()
            for icon in _card_header_icons_without_gap(text)
        )
        self.assertEqual(offenders, [], "Add me-1 to these icons, or the header shows the icon against the text")


def _htmx_tables_outside_a_container(text: str) -> int:
    """Return how many ``inc/table_htmx.html`` renders of *text* have no ``.htmx-container`` ancestor."""
    from bs4 import BeautifulSoup

    renders = BeautifulSoup(text, "html.parser").find_all(string=re.compile(r"inc/table_htmx\.html"))
    return sum(render.find_parent(class_="htmx-container") is None for render in renders)


class TestHtmxTableContainer(SimpleTestCase):
    """NetBox's ``inc/table_htmx.html`` sorts through its header, which targets the closest ``.htmx-container``."""

    def test_detector_flags_a_table_beside_a_container(self):
        bad = (
            '<div class="htmx-container"></div>\n<div class="card">{% render_table table "inc/table_htmx.html" %}</div>'
        )
        self.assertEqual(_htmx_tables_outside_a_container(bad), 1)

    def test_detector_accepts_a_table_inside_a_container(self):
        good = (
            '<div id="search" class="card htmx-container" hx-target="#search">\n'
            '  {% if table %}<div class="table-responsive">{% render_table table "inc/table_htmx.html" %}</div>'
            "{% endif %}\n</div>"
        )
        self.assertEqual(_htmx_tables_outside_a_container(good), 0)

    def test_every_htmx_table_has_a_container_for_its_sortable_header(self):
        missing = sorted(name for name, text in _plugin_templates().items() if _htmx_tables_outside_a_container(text))
        self.assertEqual(missing, [], "Without an .htmx-container ancestor, a click on a column header swaps nothing")


class TestRowActionButtons(SimpleTestCase):
    """Every htmx POST button is a table row button, so each takes the shared row attributes."""

    @staticmethod
    def _button_tags(text: str) -> list[str]:
        """Return the source from the start of each tag with ``hx-post=`` to the end of that tag."""
        return [
            text[text.rindex("<", 0, found.start()) : text.index(">", found.end())]
            for found in re.finditer("hx-post=", text)
        ]

    def test_every_hx_post_button_includes_the_row_action_attributes(self):
        sources = _plugin_templates()
        sources["tables.py"] = (Path(netbox_kea.__file__).parent / "tables.py").read_text()
        include = 'include "netbox_kea/inc/row_action_htmx.html"'
        missing = sorted(
            name for name, text in sources.items() for tag in self._button_tags(text) if include not in tag
        )
        # The lease search pushes its URL and swaps itself; a row button without the include does both.
        self.assertEqual(missing, [])


class TestTablesScrollInsideTheirContainer(SimpleTestCase):
    """A wide table must scroll inside a ``.table-responsive`` element, not make the whole page scroll."""

    def test_every_rendered_table_sits_in_a_table_responsive_element(self):
        missing = []
        for name, text in _plugin_templates().items():
            for found in re.finditer(r"{%\s*render_table\b", text):
                opening = re.findall(r"<div\b[^>]*>", text[: found.start()])
                if not (opening and "table-responsive" in opening[-1]):
                    missing.append(name)
        self.assertEqual(sorted(missing), [])
