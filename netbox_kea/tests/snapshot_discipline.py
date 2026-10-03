# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Check reviewed direct-save sites and their local snapshot sequence.

This inventory does not infer model types, helper effects or heap aliases.
See docs/design/snapshot-discipline.md for its supported scope.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MUTATORS = {
    "_update_mac_description",
    "_apply_reservation_identifier",
}


@dataclass(frozen=True)
class SaveSite:
    """One reviewed direct receiver, with its save count and classification."""

    path: str
    function: str
    receiver: str
    kind: str
    count: int = 1
    constructors: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the inventory key used by both scanner entry points."""
        return self.path, self.function, self.receiver


SITES = (
    SaveSite("ipam_reconciliation.py", "_complete_observation", "current", "loaded"),
    SaveSite("ipam_reconciliation.py", "_claim", "legacy[0]", "loaded"),
    SaveSite("ipam_reconciliation.py", "_apply_claim", "ip", "loaded"),
    SaveSite("ipam_reconciliation.py", "_restatus", "ip", "loaded"),
    SaveSite("ipam_reconciliation.py", "_remove_stale_link", "ip", "loaded"),
    SaveSite("ipam_reconciliation.py", "_claim_network", "obj", "loaded"),
    SaveSite("ipam_reconciliation.py", "_remove_stale_network_link", "locked", "loaded", count=2),
    SaveSite("sync.py", "sync_mac_address", "mac_obj", "loaded"),
    SaveSite("views/sync_jobs.py", "ServerSyncToggleView.post", "server", "loaded"),
    SaveSite("integrations/dhcp_plugin.py", "upsert_options", "obj", "loaded", constructors=("Option",)),
    SaveSite("integrations/dhcp_plugin.py", "_apply_global_settings", "dhcp_server", "loaded"),
    SaveSite("integrations/dhcp_plugin.py", "upsert_client_class", "obj", "loaded", constructors=("ClientClass",)),
    SaveSite("integrations/dhcp_plugin.py", "upsert_subnet", "existing", "loaded"),
    SaveSite("integrations/dhcp_plugin.py", "_upsert_reservation", "obj", "loaded", constructors=("HostReservation",)),
    SaveSite("integrations/dhcp_plugin.py", "_create_custom_option_def", "obj", "create"),
    SaveSite("integrations/dhcp_plugin.py", "upsert_subnet", "subnet_obj", "create"),
    SaveSite("ipam_reconciliation.py", "_claim", "ip", "create"),
    SaveSite("ipam_reconciliation.py", "_store_link", "link", "plain"),
    SaveSite("ipam_reconciliation.py", "_remove_stale_link", "link", "plain"),
    SaveSite("ipam_reconciliation.py", "_remove_stale_network_link", "link", "plain"),
    SaveSite("views/sync_jobs.py", "SyncJobsView.post", "sync_cfg", "plain"),
    SaveSite("jobs.py", "KeaIpamSyncJob.run", "self.job", "framework"),
    SaveSite("models.py", "Server.save", "super()", "framework"),
    SaveSite("models.py", "SyncConfig.save", "super()", "framework"),
)


@dataclass(frozen=True, order=True)
class Violation:
    """A source diagnostic for the local hook or a test."""

    path: str
    lineno: int
    qualname: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.qualname}: {self.message}"


@dataclass(frozen=True)
class State:
    """The snapshot cycle of one direct receiver, without alias inference."""

    protected: bool = False
    mutated: bool = False
    fresh: bool = False


def _scopes(body: list[ast.stmt], prefix: str = "") -> list[tuple[str, ast.AST]]:
    result: list[tuple[str, ast.AST]] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append((prefix + node.name, node))
        elif isinstance(node, ast.ClassDef):
            result.extend(_scopes(node.body, prefix + node.name + "."))
        else:
            result.append(("<module>", node))
    return result


class Sequence:
    """Check direct field changes and recognized mutators at one site."""

    def __init__(self, site: SaveSite):
        self.site = site
        self.hits: set[Violation] = set()

    def report(self, node: ast.AST, message: str) -> None:
        self.hits.add(Violation(self.site.path, getattr(node, "lineno", 1), self.site.function, message))

    def receiver(self, node: ast.AST) -> bool:
        return ast.unparse(node) == self.site.receiver

    @staticmethod
    def mutate(state: State) -> State:
        """Record a change to an existing receiver."""
        return state if state.fresh else State(state.protected, True)

    def expression(self, node: ast.AST, state: State, standalone: bool = False) -> State:
        for call in (part for part in ast.walk(node) if isinstance(part, ast.Call)):
            if isinstance(call.func, ast.Attribute) and self.receiver(call.func.value):
                if call.func.attr == "snapshot":
                    if not standalone or call is not node:
                        self.report(call, "snapshot requires a standalone statement")
                    elif state.mutated:
                        self.report(call, "snapshot after mutation overwrites the prechange state")
                    else:
                        state = State(True, False, state.fresh)
                elif call.func.attr == "save":
                    if not state.fresh and not state.protected:
                        self.report(call, "loaded save requires snapshot before its first mutation")
                    state = State()
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in MUTATORS | {"setattr"}
                and call.args
                and self.receiver(call.args[0])
            ):
                state = self.mutate(state)
        return state

    def block(self, body: list[ast.stmt], states: set[State]) -> set[State]:
        for node in body:
            states = {result for state in states for result in self.statement(node, state)}
        return states

    def assignment(self, node: ast.Assign | ast.AnnAssign | ast.AugAssign, state: State) -> State:
        if node.value is not None:
            state = self.expression(node.value, state)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and self.receiver(target.value):
                state = self.mutate(state)
            elif self.receiver(target):
                fresh = (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in self.site.constructors
                )
                state = State(fresh=fresh)
        return state

    def statement(self, node: ast.stmt, state: State) -> set[State]:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return {self.assignment(node, state)}
        if isinstance(node, ast.Expr):
            return {self.expression(node.value, state, standalone=True)}
        if isinstance(node, ast.If):
            state = self.expression(node.test, state)
            return self.block(node.body, {state}) | self.block(node.orelse, {state})
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return self.block(node.body, {state})
        if isinstance(node, ast.Try):
            normal = self.block(node.body, {state})
            normal = self.block(node.orelse, normal)
            handled = {result for handler in node.handlers for result in self.block(handler.body, {state})}
            return self.block(node.finalbody, normal | handled)
        if isinstance(node, (ast.For, ast.While)):
            # Existing update loops need the zero and one iteration paths only.
            entered = self.block(node.body, {state})
            return self.block(node.orelse, {state} | entered)
        if isinstance(node, (ast.Return, ast.Raise)):
            return set()
        return {state}


def scan_source(source: str, path: str = "source.py") -> list[Violation]:
    """Classify direct saves in one module and check inventoried updates."""
    tree = ast.parse(source)
    hits: list[Violation] = []
    expected = {site.key: site for site in SITES if site.path == path}
    seen: Counter[tuple[str, str, str]] = Counter()
    for function, node in _scopes(tree.body):
        saves = [
            (call, call.func.value)
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "save"
        ]
        receivers = {ast.unparse(receiver) for _, receiver in saves}
        for call, receiver in saves:
            key = path, function, ast.unparse(receiver)
            seen[key] += 1
            if key not in expected:
                hits.append(Violation(path, call.lineno, function, "direct save requires inventory classification"))
        for receiver_name in receivers:
            site = expected.get((path, function, receiver_name))
            if site is not None and site.kind == "loaded" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                checker = Sequence(site)
                checker.block(node.body, {State()})
                hits.extend(checker.hits)
    for key, site in expected.items():
        if seen[key] != site.count:
            hits.append(Violation(path, 1, site.function, "save count changed; update inventory classification"))
    return sorted(set(hits))


def scan_tree(root: Path = PACKAGE_ROOT) -> list[Violation]:
    """Use the same inventory for production modules, excluding tests/migrations."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if not {"tests", "migrations"}.intersection(relative.parts):
            hits.extend(scan_source(path.read_text(), relative.as_posix()))
    return hits


def main(root: Path = PACKAGE_ROOT) -> int:
    """Print the whole-tree hook result without importing NetBox."""
    hits = scan_tree(root)
    for hit in hits:
        print(hit)
    print(f"snapshot-discipline: {len(hits)} violation(s)")
    return int(bool(hits))


if __name__ == "__main__":
    sys.exit(main())
