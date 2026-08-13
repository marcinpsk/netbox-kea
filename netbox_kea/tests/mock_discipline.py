# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""AST 'mock discipline' guard — flag attribute-fabricating mocks used as object stand-ins.

A bare ``MagicMock()`` (or ``Mock()``) synthesises *any* attribute or method on demand,
so a test built on one stays green while the real code path is broken — e.g. a field the
production branch reads is never actually set, yet ``row.whatever`` still returns a truthy
mock. Mocks are a last resort reserved for true external boundaries you cannot run locally
(third-party network calls, paid/destructive/nondeterministic side effects), and even there
a ``spec=``-bounded mock or a real fake (a recorded HTTP fixture) beats a bare one.

Two shapes are flagged, because they produce the same object:

  * **Instantiation** — ``MagicMock()``, ``Mock()``, ``AsyncMock()`` and friends.
  * **Patching our own code** — ``patch("netbox_kea.x.y")`` or ``patch.object(Server, "z")``
    without ``autospec=``. ``patch`` hands back a plain ``MagicMock``, so the same
    fabrication applies and the test also survives a signature change in the real function.
    Only ``netbox_kea`` targets count: patching ``requests.Session.post`` is the endorsed
    way to stub the one boundary the suite cannot run.

This scanner is deliberately a bit too aggressive: it flags every such call, then lets you
carve out the legitimate cases three ways —

  1. **Bound it.** ``MagicMock(spec=KeaClient)`` / ``spec_set=`` / ``create_autospec`` /
     ``wraps=real_obj`` restrict (or delegate) attribute access to a real interface, so the
     fabrication footgun is gone. For ``patch`` that is ``autospec=True``, a ready-made
     ``new=``, a non-mock ``new_callable=``, or a mock factory with a spec. These are never
     flagged.
  2. **Mark it.** An inline ``# mock-ok: <reason>`` comment on the statement records a
     reviewed, deliberate boundary mock. Preferred for new code — it documents *why*.
  3. **Grandfather it.** ``netbox_kea/tests/mock_discipline_baseline.txt`` records how many
     unbounded calls are currently accepted per (file, function), counting both shapes
     together. New ones beyond the recorded count fail the guard. Regenerate after an
     intentional change with::

         python3 netbox_kea/tests/mock_discipline.py --update-baseline

``AsyncMock`` is flagged here too (``INCLUDE_ASYNCMOCK = True``). It is normally left off —
in async-heavy code it is the idiomatic awaitable stub, and flagging all of them buries the
signal — but this plugin is entirely synchronous (no ``async def``/``await``/``asyncio``), so
an ``AsyncMock`` is almost always a mistake and worth catching. Flip it back to ``False`` if
real async boundaries are introduced. Tune the policy by editing the constants below.

Stdlib-only by design: this module imports nothing from ``netbox_kea`` (which would pull in
NetBox/Django), so it runs as a standalone pre-commit hook on the host without a NetBox
install. Run it directly (``python3 netbox_kea/tests/mock_discipline.py``); the pytest suite
also imports it as ``netbox_kea.tests.mock_discipline`` (see ``test_mock_discipline.py``).
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
_BASELINE_PATH = TESTS_ROOT / "mock_discipline_baseline.txt"

# Mock classes that fabricate arbitrary attributes when unspecced — the dangerous kind.
_FABRICATING_MOCKS = {"MagicMock", "NonCallableMagicMock", "Mock", "NonCallableMock"}
# Also flag AsyncMock: this plugin is sync-only, so an awaitable stub is almost always a
# mistake. Set False if real async boundaries are introduced (then it would bury the signal).
INCLUDE_ASYNCMOCK = True
# Keyword args that bound a mock to a real interface (or delegate to a real object).
_BOUNDING_KWARGS = {"spec", "spec_set", "autospec", "wraps"}
# The same for patch(), which additionally accepts a ready-made replacement object.
_PATCH_BOUNDING_KWARGS = _BOUNDING_KWARGS | {"new"}
# Canonical values stored in the lexical binding table for module imports.
_MOCK_MODULE = "unittest.mock"
_UNITTEST_MODULE = "unittest"
# Import prefix that marks a patch target as our own code rather than a real boundary.
_FIRST_PARTY = "netbox_kea"
# Inline opt-out marker (in a comment): `# mock-ok` or `# mock-ok: reason`.
_MARKER = "mock-ok"
# Files the scanner never inspects (itself + its own test).
_SELF = {"mock_discipline.py", "test_mock_discipline.py"}


def _targets() -> set[str]:
    return _FABRICATING_MOCKS | ({"AsyncMock"} if INCLUDE_ASYNCMOCK else set())


@dataclass(frozen=True)
class Violation:
    """One flagged mock instantiation or unspecced first-party patch."""

    path: str  # posix relpath from tests/
    lineno: int
    qualname: str  # enclosing function/class path, or "<module>"
    mock: str  # the mock class name, or the patch target for kind="patch"
    kind: str = "mock"  # "mock" (a fabricating class) or "patch" (an unspecced patch)

    @property
    def site(self) -> str:
        """Stable (line-independent) key used by the baseline: file + enclosing scope."""
        return f"{self.path}::{self.qualname}"

    def __str__(self) -> str:
        if self.kind == "patch":
            return f"{self.path}:{self.lineno}: unspecced patch of first-party {self.mock} in {self.qualname}()"
        return f"{self.path}:{self.lineno}: unapproved {self.mock}() in {self.qualname}()"


def _comment_lines(src: str) -> dict[int, str]:
    """Map line-number → comment text for every real comment token (string-safe)."""
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return comments


class _MockBindingCollector(ast.NodeVisitor):
    """Record mock imports and shadowing assignments in each lexical scope."""

    def __init__(self, module: ast.AST) -> None:
        self.bindings: dict[ast.AST, dict[str, list[tuple[int, str | None]]]] = {}
        self._scopes: list[ast.AST] = [module]

    def _bind(self, name: str, lineno: int, canonical: str | None = None) -> None:
        self.bindings.setdefault(self._scopes[-1], {}).setdefault(name, []).append((lineno, canonical))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            canonical = None
            if node.module == "unittest.mock":
                canonical = alias.name
            elif node.module == "unittest" and alias.name == "mock":
                canonical = _MOCK_MODULE
            self._bind(alias.asname or alias.name, node.lineno, canonical)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            canonical = None
            if alias.name == "unittest.mock":
                canonical = _MOCK_MODULE if alias.asname else _UNITTEST_MODULE
            elif alias.name == "unittest":
                canonical = _UNITTEST_MODULE
            self._bind(name, node.lineno, canonical)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._bind(node.id, node.lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind(node.name, node.lineno)
        self._scopes.append(node)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            self._bind(arg.arg, node.lineno)
        if node.args.vararg:
            self._bind(node.args.vararg.arg, node.lineno)
        if node.args.kwarg:
            self._bind(node.args.kwarg.arg, node.lineno)
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name, node.lineno)
        self._scopes.append(node)
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, node.lineno)
        self.generic_visit(node)


def _mock_bindings(tree: ast.AST) -> dict[ast.AST, dict[str, list[tuple[int, str | None]]]]:
    """Collect mock imports and shadowing bindings without leaking between scopes."""
    collector = _MockBindingCollector(tree)
    collector.visit(tree)
    return collector.bindings


def _first_party_names(tree: ast.AST) -> set[str]:
    """Local names bound to something imported from this plugin.

    Lets ``patch.object(Server, "get_client")`` be recognised as patching our own code,
    the same as the dotted-string form ``patch("netbox_kea.models.Server.get_client")``.
    Relative imports count: every test module lives inside the package.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0 or module.split(".")[0] == _FIRST_PARTY:
                names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _FIRST_PARTY:
                    names.add((alias.asname or alias.name).split(".")[0])
    return names


class _Scanner(ast.NodeVisitor):
    """Collect fabricating-mock instantiations that are neither bounded nor marked."""

    def __init__(
        self,
        rel: str,
        comments: dict[int, str],
        bindings: dict[ast.AST, dict[str, list[tuple[int, str | None]]]],
        module: ast.AST,
        first_party: set[str],
    ):
        self._rel = rel
        self._comments = comments
        self._bindings = bindings
        self._first_party = first_party
        self._scope: list[str] = []
        self._binding_scopes: list[ast.AST] = [module]
        self.hits: list[Violation] = []

    # ── scope tracking ────────────────────────────────────────────────────────
    def _qual(self) -> str:
        return ".".join(self._scope) or "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)

        header_expressions = [*node.decorator_list, *node.args.defaults]
        header_expressions.extend(default for default in node.args.kw_defaults if default is not None)
        header_expressions.extend(
            arg.annotation
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if arg.annotation is not None
        )
        if node.args.vararg and node.args.vararg.annotation:
            header_expressions.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            header_expressions.append(node.args.kwarg.annotation)
        if node.returns:
            header_expressions.append(node.returns)
        header_expressions.extend(getattr(node, "type_params", ()))
        for expression in header_expressions:
            self.visit(expression)

        self._binding_scopes.append(node)
        for statement in node.body:
            self.visit(statement)
        self._binding_scopes.pop()
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self._binding_scopes.append(node)
        self.generic_visit(node)
        self._binding_scopes.pop()
        self._scope.pop()

    # ── the check ─────────────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call) -> None:
        name = self._mock_class(node.func)
        if name and not self._is_bounded(node) and not self._is_marked(node):
            self.hits.append(Violation(self._rel, node.lineno, self._qual(), name))
        target = self._unspecced_first_party_patch(node)
        if target and not self._is_marked(node):
            self.hits.append(Violation(self._rel, node.lineno, self._qual(), target, kind="patch"))
        self.generic_visit(node)

    def _unspecced_first_party_patch(self, node: ast.Call) -> str | None:
        """Return the patched first-party target, or None if this call is fine.

        ``patch("netbox_kea.x.y")`` hands the caller a plain ``MagicMock``: it accepts any
        signature and fabricates any attribute, so the test keeps passing after the real
        function changes shape. ``autospec=True`` (or ``spec``/``new``/``wraps``) binds
        the replacement to the real object instead. A non-mock ``new_callable`` is also
        accepted. Only first-party targets are flagged: patching
        ``requests.Session.post`` is the endorsed way to stub the one boundary the suite
        cannot run.
        """
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "object":
            # patch.object(target, "attribute"[, new]) — a third positional is `new`.
            if not self._is_patch(func.value) or self._is_patch_bounded(node, new_position=2):
                return None
            root = node.args[0] if node.args else None
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in self._first_party:
                return ast.unparse(node.args[0])
            return None
        # patch("dotted.target"[, new]) — a second positional is `new`.
        if not self._is_patch(func) or self._is_patch_bounded(node, new_position=1):
            return None
        target = node.args[0] if node.args else None
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if target.value.split(".")[0] == _FIRST_PARTY:
                return repr(target.value)
        return None

    def _is_patch(self, func: ast.expr) -> bool:
        """True for ``patch``, an aliased import of it, or ``<module>.patch``."""
        if isinstance(func, ast.Attribute):
            return func.attr == "patch" and self._canonical_binding(func.value) == _MOCK_MODULE
        return self._canonical_binding(func) == "patch"

    def _is_patch_bounded(self, node: ast.Call, new_position: int) -> bool:
        replacement = next((kw.value for kw in node.keywords if kw.arg == "new"), None)
        if replacement is None and len(node.args) > new_position:
            replacement = node.args[new_position]
        if replacement is not None and not self._is_mock_default(replacement):
            return True

        for kw in node.keywords:
            if kw.arg not in _PATCH_BOUNDING_KWARGS or kw.arg == "new":
                continue
            # None is the default. False explicitly disables spec arguments.
            if isinstance(kw.value, ast.Constant) and (kw.value.value is None or kw.value.value is False):
                continue
            return True

        factory = next((kw.value for kw in node.keywords if kw.arg == "new_callable"), None)
        if factory is None or (isinstance(factory, ast.Constant) and factory.value is None):
            return False
        return self._mock_class(factory) is None

    def _canonical_binding(self, node: ast.expr) -> str | None:
        """Resolve a mock import at this source location through the lexical scopes."""
        if isinstance(node, ast.Attribute) and node.attr == "mock":
            if self._canonical_binding(node.value) == _UNITTEST_MODULE:
                return _MOCK_MODULE
            return None
        if not isinstance(node, ast.Name):
            return None

        inside_function = False
        for scope in reversed(self._binding_scopes):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inside_function = True
            elif inside_function and isinstance(scope, ast.ClassDef):
                # Method bodies do not close over their class namespace.
                continue
            events = self._bindings.get(scope, {}).get(node.id, [])
            prior = [event for event in events if event[0] <= node.lineno]
            if prior:
                return prior[-1][1]
            if events and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
        return None

    def _is_mock_default(self, node: ast.expr) -> bool:
        """True only for ``unittest.mock.DEFAULT``, including imported aliases."""
        if isinstance(node, ast.Attribute) and node.attr == "DEFAULT":
            return self._canonical_binding(node.value) == _MOCK_MODULE
        return self._canonical_binding(node) == "DEFAULT"

    def _mock_class(self, func: ast.expr) -> str | None:
        targets = _targets()
        if isinstance(func, ast.Attribute) and func.attr in targets:
            if self._canonical_binding(func.value) == _MOCK_MODULE:
                return func.attr  # e.g. mock.MagicMock(...), unittest.mock.MagicMock(...)
            return None
        canonical = self._canonical_binding(func)
        if canonical in targets:
            return canonical  # imported (possibly aliased) name
        return None

    @staticmethod
    def _is_bounded(node: ast.Call) -> bool:
        return any(kw.arg in _BOUNDING_KWARGS for kw in node.keywords)

    def _is_marked(self, node: ast.Call) -> bool:
        # A `# mock-ok` marker counts if it's a trailing/inline comment anywhere in the
        # call's own line span, or in a contiguous comment block directly above the line
        # (so the reason can be written above the mock, the way people naturally do).
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if any(_MARKER in self._comments.get(ln, "") for ln in range(node.lineno, end + 1)):
            return True
        ln = node.lineno - 1
        while ln in self._comments:
            if _MARKER in self._comments[ln]:
                return True
            ln -= 1
        return False


def scan_source(src: str, rel: str = "<source>") -> list[Violation]:
    """Scan one module's source text and return its mock-discipline violations."""
    tree = ast.parse(src, filename=rel)
    scanner = _Scanner(
        rel,
        _comment_lines(src),
        _mock_bindings(tree),
        tree,
        _first_party_names(tree),
    )
    scanner.visit(tree)
    return scanner.hits


def scan_tree(root: Path = TESTS_ROOT) -> list[Violation]:
    """Scan every test module under *root* (skipping the guard's own files)."""
    out: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in _SELF or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        out.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return out


def _counts_by_site(violations: list[Violation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in violations:
        counts[v.site] = counts.get(v.site, 0) + 1
    return counts


def load_baseline(path: Path = _BASELINE_PATH) -> dict[str, int]:
    """Read the grandfathered per-site allowance (``site\\tcount`` lines)."""
    if not path.exists():
        return {}
    allowed: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        site, _, count = line.rpartition("\t")
        allowed[site] = int(count)
    return allowed


def save_baseline(counts: dict[str, int], path: Path = _BASELINE_PATH) -> None:
    """Write the per-site allowance file (sorted, with an explanatory header)."""
    # REUSE-IgnoreStart — these literals are the *generated* baseline header, not this
    # file's own SPDX tags; without the guard REUSE misparses the embedded identifier.
    header = [
        "# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>",
        "# SPDX-License-Identifier: Apache-2.0",
        "# Mock-discipline baseline — grandfathered spec-less MagicMock/Mock usages.",
        "# Each line: <relpath-from-netbox_kea/tests>::<qualname>\\t<allowed-count>.",
        "# Shrink this file over time: replace a mock with a real object or a spec=-bounded",
        "# mock, or add an inline `# mock-ok: <reason>`. Regenerate after an intentional",
        "# change with:  python3 netbox_kea/tests/mock_discipline.py --update-baseline",
        "",
    ]
    # REUSE-IgnoreEnd
    body = [f"{site}\t{counts[site]}" for site in sorted(counts)]
    # rstrip so an empty body (no grandfathered mocks) doesn't leave a trailing blank
    # line that the end-of-file-fixer hook would strip on the next commit.
    path.write_text("\n".join(header + body).rstrip("\n") + "\n", encoding="utf-8")


def unapproved(root: Path = TESTS_ROOT, baseline: dict[str, int] | None = None) -> list[Violation]:
    """Return violations beyond the baseline allowance, sorted by file then line."""
    allowed = load_baseline() if baseline is None else baseline
    by_site: dict[str, list[Violation]] = {}
    for v in scan_tree(root):
        by_site.setdefault(v.site, []).append(v)
    extra: list[Violation] = []
    for site, found in by_site.items():
        budget = allowed.get(site, 0)
        if len(found) > budget:
            # Report the excess (the newest-by-line ones beyond the grandfathered count).
            extra.extend(sorted(found, key=lambda v: v.lineno)[budget:])
    return sorted(extra, key=lambda v: (v.path, v.lineno))


def _main(argv: list[str]) -> int:
    if "--update-baseline" in argv:
        counts = _counts_by_site(scan_tree())
        save_baseline(counts)
        print(f"baseline updated: {sum(counts.values())} mock(s) grandfathered across {len(counts)} site(s)")
        return 0
    bad = unapproved()
    for v in bad:
        print(str(v))
    print(f"\n{len(bad)} unapproved mock(s)")
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint; _main() is unit-tested
    import sys

    raise SystemExit(_main(sys.argv[1:]))
