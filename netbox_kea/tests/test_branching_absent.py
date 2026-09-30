# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Without netbox-branching, the plugin's branching functions do nothing."""

import ast
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_kea import branching
from netbox_kea.tests.kea_stub import stub_kea
from netbox_kea.tests.utils import _make_db_server

if branching.installed():
    pytest.skip("netbox-branching is an installed app: test_branching.py covers this run", allow_module_level=True)


class WithoutBranchingTest(SimpleTestCase):
    """netbox-branching stays optional."""

    def test_no_branch_is_active(self):
        self.assertIsNone(branching.active_branch())

    def test_refuse_in_branch_does_nothing(self):
        self.assertIsNone(branching.refuse_in_branch("a test change"))

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


class MiddlewareWithoutBranchingTest(TestCase):
    """The refusal middleware runs, and a request that names a branch is served as before."""

    def test_the_middleware_is_installed(self):
        self.assertIn("netbox_kea.branching.BranchRefusalMiddleware", settings.MIDDLEWARE)

    def test_a_change_that_names_a_branch_is_served_without_a_sources_header(self):
        self.client.force_login(get_user_model().objects.create_superuser("no-branching"))
        server = _make_db_server(ca_url="https://before.example.com", dhcp6=False)
        self.client.cookies["active_branch"] = "abcd1234"
        url = f"{reverse('plugins:netbox_kea:server_edit', args=[server.pk])}?_branch=abcd1234"

        with stub_kea({"version-get": {"result": 0, "arguments": {"extended": "3.2.0"}}}) as kea:
            response = self.client.post(
                url, {"name": server.name, "ca_url": "https://after.example.com", "dhcp4": True, "ssl_verify": True}
            )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(branching.SOURCES_HEADER, response.headers)
        self.assertEqual(kea.commands(), ["version-get"])
        server.refresh_from_db()
        self.assertEqual(server.ca_url, "https://after.example.com")


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
