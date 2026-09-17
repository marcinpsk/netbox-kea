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

    def _imported_names(self) -> set[str]:
        tree = ast.parse((_VIEWS_PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        return {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
            for alias in node.names
        }

    def test_the_package_imports_every_view_module(self):
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

    def test_the_package_imports_nothing_that_is_gone(self):
        """A name left behind after a module is deleted breaks the package import."""
        self.assertEqual(sorted(self._imported_names() - self._module_names()), [])

    def test_the_guard_reads_a_real_module_list(self):
        """Two empty sets would make the tests above pass without checking anything."""
        self.assertGreater(len(self._module_names()), 5)
        self.assertEqual(self._imported_names(), self._module_names())
