# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Without netbox-branching, the plugin's branching functions do nothing."""

import ast
import sys
from pathlib import Path

import pytest
from django.test import SimpleTestCase

from netbox_kea import branching

if branching.installed():
    pytest.skip("netbox-branching is an installed app: test_branching.py covers this run", allow_module_level=True)


class WithoutBranchingTest(SimpleTestCase):
    """netbox-branching stays optional."""

    def test_no_branch_is_active(self):
        self.assertIsNone(branching.active_branch())

    def test_register_does_not_import_netbox_branching(self):
        branching.register()

        self.assertNotIn("netbox_branching", sys.modules)

    def test_only_the_branching_module_imports_netbox_branching(self):
        package = Path(branching.__file__).parent
        importers = sorted(
            str(path.relative_to(package))
            for path in package.rglob("*.py")
            if "tests" not in path.relative_to(package).parts and _imports_netbox_branching(path)
        )

        self.assertEqual(importers, ["branching.py"])


def _imports_netbox_branching(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if any(module.split(".")[0] == "netbox_branching" for module in modules):
            return True
    return False
