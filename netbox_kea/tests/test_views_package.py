# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the views package's one job: importing every view module."""

import ast
from pathlib import Path

from django.test import SimpleTestCase

from netbox_kea import views

_VIEWS_PACKAGE = Path(views.__file__).parent


class TestViewsPackageImportsEveryModule(SimpleTestCase):
    def _module_names(self) -> set[str]:
        return {path.stem for path in _VIEWS_PACKAGE.glob("*.py") if path.stem != "__init__"}

    @staticmethod
    def _package_level_imports(tree: ast.Module) -> set[str]:
        """Submodules the package body imports, in every spelling that runs their code.

        Only a direct statement of ``tree.body``, which is stricter than "what runs": an
        import the package body reaches through a ``try:`` or an ``if`` does run, and this
        does not count it. Nothing in the package imports that way, and the alternative
        counts an ``if TYPE_CHECKING:`` import, which runs no register_model_view
        decorator at all. Every import spelling, though, because ``from .leases import X``
        imports ``leases`` just as ``from . import leases`` does, and reading only the
        second would call the first a missing module.
        """
        package = views.__name__
        found: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                found.update(
                    alias.name[len(package) + 1 :].split(".")[0]
                    for alias in node.names
                    if alias.name.startswith(f"{package}.")
                )
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if node.level == 1:
                source = module
            elif node.level == 0 and (module == package or module.startswith(f"{package}.")):
                source = module[len(package) + 1 :]
            else:
                continue
            # `from . import x` names the submodules; `from .x import Thing` imports x.
            found.update({source.split(".")[0]} if source else {alias.name for alias in node.names})
        return found

    def _imported_names(self) -> set[str]:
        return self._package_level_imports(ast.parse((_VIEWS_PACKAGE / "__init__.py").read_text(encoding="utf-8")))

    def test_the_package_imports_every_view_module(self) -> None:
        """A module the package skips never runs its register_model_view decorators.

        urls.py asks get_model_urls for the Server tabs, which only knows the views that
        registered themselves while their module was imported. A new module left out of
        __init__.py loses its tabs with no import error and no failing URL until someone
        opens the tab. This reads the source rather than sys.modules, because a module
        another view happens to import would make that check pass for the wrong reason.
        """
        missing = sorted(self._module_names() - self._imported_names())

        self.assertEqual(
            missing,
            [],
            f"netbox_kea/views/__init__.py does not import {missing}, so their "
            "register_model_view decorators never run and their Server tabs disappear.",
        )

    def test_the_package_imports_nothing_that_is_gone(self) -> None:
        """A name left behind after a module is deleted breaks the package import."""
        self.assertEqual(sorted(self._imported_names() - self._module_names()), [])

    def test_an_import_the_package_body_never_runs_does_not_count(self) -> None:
        """A deferred import registers no view, so it must not satisfy the guard."""
        source = (
            "from typing import TYPE_CHECKING\n"
            "from . import runs_at_import\n"
            "if TYPE_CHECKING:\n"
            "    from . import only_for_type_checkers\n"
            "def _later():\n"
            "    from . import only_inside_a_function\n"
        )

        self.assertEqual(self._package_level_imports(ast.parse(source)), {"runs_at_import"})

    def test_every_package_level_import_spelling_counts(self) -> None:
        """`from .leases import X` runs leases too, so reading only `from . import x` lies."""
        source = (
            "from . import plain\n"
            "from .dotted import SomeView\n"
            "from netbox_kea.views import absolute\n"
            "from netbox_kea.views.absolute_dotted import OtherView\n"
            "import netbox_kea.views.imported\n"
        )

        self.assertEqual(
            self._package_level_imports(ast.parse(source)),
            {"plain", "dotted", "absolute", "absolute_dotted", "imported"},
        )

    def test_the_guard_reads_a_real_module_list(self) -> None:
        """Two empty sets would make the tests above pass without checking anything."""
        self.assertGreater(len(self._module_names()), 5)
        self.assertEqual(self._imported_names(), self._module_names())
