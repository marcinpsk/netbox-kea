# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Controls for the explicit direct-save snapshot inventory."""

import ast
import copy
import re

import pytest
import yaml

from netbox_kea.tests.snapshot_discipline import MUTATORS, PACKAGE_ROOT, SITES, _scopes, scan_source, scan_tree


def test_new_direct_save_requires_inventory_classification():
    source = "from ipam.models import IPAddress\ndef update():\n    obj=IPAddress.objects.get(pk=1)\n    obj.snapshot()\n    obj.status='active'\n    obj.save()\n"
    assert any("classification" in hit.message for hit in scan_source(source, "new_writer.py"))


def test_an_added_save_changes_the_reviewed_site_count():
    path = "ipam_reconciliation.py"
    original = (PACKAGE_ROOT / path).read_text()
    tree = ast.parse(original)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_complete_observation"
    )
    function.body.extend(ast.parse("current.snapshot()\ncurrent.sync_enabled=False\ncurrent.save()").body)
    assert any("classification" in hit.message for hit in scan_source(ast.unparse(tree), path))


def _observation_update(body):
    source = (PACKAGE_ROOT / "ipam_reconciliation.py").read_text()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_complete_observation"
    )
    function.body = ast.parse("current=Server.objects.get(pk=server.pk)\n" + body).body
    return ast.unparse(tree)


def test_direct_receiver_with_early_snapshot_passes():
    source = _observation_update("current.snapshot()\ncurrent.sync_enabled=False\ncurrent.save()")
    assert scan_source(source, "ipam_reconciliation.py") == []


@pytest.mark.parametrize(
    "body, diagnostic",
    [
        ("current.sync_enabled=False\ncurrent.save()", "loaded save requires snapshot"),
        ("current.sync_enabled=False\ncurrent.snapshot()\ncurrent.save()", "snapshot after mutation"),
        (
            "if workflow:\n    current.snapshot()\ncurrent.sync_enabled=False\ncurrent.save()",
            "loaded save requires snapshot",
        ),
        ("server.snapshot()\ncurrent.sync_enabled=False\ncurrent.save()", "loaded save requires snapshot"),
        ("workflow and current.snapshot()\ncurrent.sync_enabled=False\ncurrent.save()", "standalone statement"),
    ],
)
def test_nearby_direct_updates_need_their_own_early_snapshot(body, diagnostic):
    assert any(diagnostic in hit.message for hit in scan_source(_observation_update(body), "ipam_reconciliation.py"))


@pytest.mark.parametrize("helper", sorted(MUTATORS))
def test_recognized_mutators_start_the_direct_receiver_change(helper):
    good = _observation_update(f"current.snapshot()\n{helper}(current)\ncurrent.save()")
    bad = _observation_update(f"{helper}(current)\ncurrent.snapshot()\ncurrent.save()")
    assert scan_source(good, "ipam_reconciliation.py") == []
    assert any("snapshot after mutation" in hit.message for hit in scan_source(bad, "ipam_reconciliation.py"))


def test_inventory_accounts_for_loaded_creates_and_bookkeeping():
    assert sum(site.count for site in SITES if site.kind == "loaded") == 16
    assert sum(site.count for site in SITES if site.kind != "loaded") == 11
    assert {site.kind for site in SITES} == {"loaded", "create", "plain", "framework"}


def test_production_tree_has_no_snapshot_violations():
    assert scan_tree() == []


def _snapshot_sites():
    sites = []
    loaded = {(site.path, site.function, site.receiver) for site in SITES if site.kind == "loaded"}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if {"tests", "migrations"}.intersection(path.relative_to(PACKAGE_ROOT).parts):
            continue
        for function, scope in _scopes(ast.parse(path.read_text()).body):
            sites.extend(
                (path, node.lineno, ast.unparse(node.value.func.value))
                for node in ast.walk(scope)
                if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "snapshot"
                and (relative, function, ast.unparse(node.value.func.value)) in loaded
            )
    return sites


@pytest.mark.parametrize("late", [False, True], ids=["removed", "moved-after-mutation"])
def test_every_inventoried_direct_save_snapshot_is_load_bearing(late):
    sites = _snapshot_sites()
    assert len(sites) == 16
    for path, lineno, receiver in sites:
        tree = ast.parse(path.read_text())
        snapshot = next(node for node in ast.walk(tree) if isinstance(node, ast.Expr) and node.lineno == lineno)

        class MoveSnapshot(ast.NodeTransformer):
            def visit_Expr(self, node, snapshot=snapshot, receiver=receiver, lineno=lineno):
                if node is snapshot:
                    return ast.copy_location(ast.Pass(), node)
                if (
                    late
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and (node.value.func.attr == "save")
                    and (ast.unparse(node.value.func.value) == receiver)
                    and (node.lineno > lineno)
                ):
                    return [copy.deepcopy(snapshot), node]
                return self.generic_visit(node)

        changed = ast.unparse(MoveSnapshot().visit(tree))
        hits = scan_source(changed, path.relative_to(PACKAGE_ROOT).as_posix())
        assert hits, (path.name, receiver, late)
        assert any("snapshot" in hit.message or "mutation" in hit.message for hit in hits), hits


def test_hook_scans_the_whole_production_tree():
    root = PACKAGE_ROOT.parent
    configuration = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook for repo in configuration["repos"] for hook in repo["hooks"] if hook["id"] == "snapshot-discipline"
    )
    assert hook["entry"] == "python3 netbox_kea/tests/snapshot_discipline.py"
    assert hook["pass_filenames"] is False
    assert re.search(hook["files"], "netbox_kea/new_directory/new_writer.py")
    assert re.search(hook["files"], ".pre-commit-config.yaml")


def test_scan_tree_excludes_test_and_migration_fixtures(tmp_path):
    for name in ("tests/bad.py", "migrations/bad.py"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("def update(obj):\n    obj.save()\n")
    assert scan_tree(tmp_path) == []
    (tmp_path / "writer.py").write_text("def update(obj):\n    obj.save()\n")
    assert any(hit.path == "writer.py" for hit in scan_tree(tmp_path))
