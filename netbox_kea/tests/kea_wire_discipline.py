# SPDX-FileCopyrightText: 2026 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0
"""Keep Kea wire literals inside their owning modules, with budgets for existing debt.

The vocabulary covers command names, hyphenated payload keys (including option names),
and family keys or service/command f-strings. Bare dhcp4/dhcp6 are model fields.
Only arguments also counts in subscripts, .get(), and positional helper calls whose
first argument is a Name. Bare arguments strings do not count.
Reads through a variable are out of reach; command names catch those wire sites.
Command templates (f-strings, .format, %, +) require a complete command-family
prefix before the first hyphen.
The family may interpolate its protocol number, as in subnet{version}-list.
A hole may be empty. Only the outermost template is checked, and then its holes.
Quoted wire literals in Django template tags count. Proven filter/exclude keyword
names are model fields, including service-shaped f-strings used only as those names.
"""

from __future__ import annotations

import argparse
import ast
import re
import string
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_PATH = Path(__file__).with_name("kea_wire_discipline_baseline.txt")
_OWNERS = frozenset({"kea.py", "server_configuration.py", "subnet_catalogue.py", "reservations.py", "dhcp_options.py"})

# Frozen from owner command sites and hyphenated literals in the follow-up brief.
WIRE_COMMANDS = frozenset(
    {
        "config-get",
        "config-set",
        "config-test",
        "config-write",
        "dhcp-disable",
        "dhcp-enable",
        "ha-heartbeat",
        "lease4-add",
        "lease4-del",
        "lease4-get",
        "lease4-get-all",
        "lease4-get-by-client-id",
        "lease4-get-by-hostname",
        "lease4-get-by-hw-address",
        "lease4-get-by-state",
        "lease4-get-page",
        "lease4-update",
        "lease4-wipe",
        "lease6-add",
        "lease6-del",
        "lease6-get",
        "lease6-get-all",
        "lease6-get-by-duid",
        "lease6-get-by-hostname",
        "lease6-get-by-state",
        "lease6-get-page",
        "lease6-update",
        "lease6-wipe",
        "list-commands",
        "network4-add",
        "network4-del",
        "network4-list",
        "network4-subnet-add",
        "network4-subnet-del",
        "network6-add",
        "network6-del",
        "network6-list",
        "network6-subnet-add",
        "network6-subnet-del",
        "reservation-add",
        "reservation-del",
        "reservation-get",
        "reservation-get-by-hostname",
        "reservation-get-page",
        "reservation-update",
        "stat-lease4-get",
        "stat-lease6-get",
        "statistic-get-all",
        "status-get",
        "subnet4-add",
        "subnet4-del",
        "subnet4-delta-add",
        "subnet4-delta-del",
        "subnet4-get",
        "subnet4-list",
        "subnet4-pool-add",
        "subnet4-pool-del",
        "subnet4-update",
        "subnet6-add",
        "subnet6-del",
        "subnet6-delta-add",
        "subnet6-delta-del",
        "subnet6-get",
        "subnet6-list",
        "subnet6-pool-add",
        "subnet6-pool-del",
        "subnet6-update",
        "version-get",
    }
)

_WIRE_COMMAND_FAMILIES = frozenset(command.split("-", 1)[0] for command in WIRE_COMMANDS)

WIRE_PAYLOAD_KEYS = frozenset(
    {
        "aftr-name",
        "all-subnets-local",
        "always-send",
        "arp-cache-timeout",
        "assigned-addresses",
        "assigned-nas",
        "assigned-pds",
        "associated-ip",
        "auto-config",
        "bcmcs-server-addr",
        "bcmcs-server-dns",
        "bcms-controller-address",
        "bcms-controller-names",
        "boot-file-name",
        "boot-size",
        "bootfile-param",
        "bootfile-url",
        "broadcast-address",
        "cache-max-age",
        "cache-threshold",
        "calculate-tee-times",
        "capwap-ac-v4",
        "circuit-id",
        "classless-static-route",
        "client-arch-type",
        "client-class",
        "client-classes",
        "client-data",
        "client-fqdn",
        "client-id",
        "client-last-transaction-time",
        "client-linklayer-addr",
        "client-ndi",
        "client-system",
        "clt-time",
        "connecting-clients",
        "connection-interrupted",
        "cookie-servers",
        "csv-format",
        "ddns-conflict-resolution-mode",
        "ddns-generated-prefix",
        "ddns-override-client-update",
        "ddns-override-no-update",
        "ddns-qualifying-suffix",
        "ddns-replace-client-name",
        "ddns-send-updates",
        "ddns-ttl",
        "ddns-ttl-max",
        "ddns-ttl-min",
        "ddns-ttl-percent",
        "ddns-update-on-renew",
        "decline-probation-period",
        "declined-addresses",
        "default-ip-ttl",
        "default-tcp-ttl",
        "default-url",
        "dhcp-agent-options",
        "dhcp-client-identifier",
        "dhcp-lease-time",
        "dhcp-max-message-size",
        "dhcp-message",
        "dhcp-message-type",
        "dhcp-option-overload",
        "dhcp-parameter-request-list",
        "dhcp-rebinding-time",
        "dhcp-renewal-time",
        "dhcp-requested-address",
        "dhcp-server-identifier",
        "dhcp4o6-server-addr",
        "dns-servers",
        "domain-name",
        "domain-name-servers",
        "domain-search",
        "echo-client-id",
        "erp-local-domain-name",
        "ethernet-encapsulation",
        "evaluate-additional-classes",
        "extensions-path",
        "finger-server",
        "flex-id",
        "font-servers",
        "geoconf-civic",
        "ha-mode",
        "ha-servers",
        "high-availability",
        "hooks-libraries",
        "host-name",
        "host-reservation-identifiers",
        "hostname-char-replacement",
        "hostname-char-set",
        "hw-address",
        "identifier-type",
        "impress-servers",
        "in-touch",
        "inf-max-rt",
        "information-refresh-time",
        "interface-id",
        "interface-mtu",
        "ip-address",
        "ip-addresses",
        "ip-forwarding",
        "irc-server",
        "last-state",
        "log-servers",
        "lpr-servers",
        "lq-client-link",
        "lq-query",
        "lq-relay-data",
        "mask-supplier",
        "match-client-id",
        "max-dgram-reassembly",
        "max-period",
        "max-preferred-lifetime",
        "max-valid-lifetime",
        "merit-dump",
        "min-preferred-lifetime",
        "min-valid-lifetime",
        "mobile-ip-home-agent",
        "name-servers",
        "name-service-search",
        "nds-context",
        "nds-servers",
        "nds-tree-name",
        "netbios-dd-server",
        "netbios-name-servers",
        "netbios-node-type",
        "netbios-scope",
        "netinfo-server-address",
        "netinfo-server-tag",
        "never-send",
        "new-posix-timezone",
        "new-tzdb-timezone",
        "next-server",
        "nis-domain",
        "nis-domain-name",
        "nis-servers",
        "nisp-domain-name",
        "nisp-servers",
        "nisplus-domain-name",
        "nisplus-servers",
        "nntp-server",
        "non-local-source-routing",
        "ntp-servers",
        "nwip-domain-name",
        "nwip-suboptions",
        "offer-lifetime",
        "only-if-required",
        "only-in-additional-list",
        "option-data",
        "option-def",
        "pana-agent",
        "path-mtu-aging-timeout",
        "path-mtu-plateau-table",
        "pd-allocator",
        "pd-exclude",
        "pd-pools",
        "perform-mask-discovery",
        "policy-filter",
        "pop-server",
        "preferred-lifetime",
        "rapid-commit",
        "rdnss-selection",
        "rebind-timer",
        "reconf-accept",
        "reconf-msg",
        "record-types",
        "relay-supplied-options",
        "remote-id",
        "renew-timer",
        "require-client-classes",
        "reservations-global",
        "reservations-in-subnet",
        "reservations-out-of-pool",
        "resource-location-servers",
        "result-set",
        "root-path",
        "router-discovery",
        "router-solicitation-address",
        "server-hostname",
        "server-id",
        "shared-network-name",
        "shared-networks",
        "sip-server-addr",
        "sip-server-dns",
        "sip-ua-cs-domains",
        "slp-directory-agent",
        "slp-service-scope",
        "smtp-server",
        "sntp-servers",
        "solmax-rt",
        "source-index",
        "static-routes",
        "status-code",
        "store-extended-info",
        "streettalk-directory-assistance-server",
        "streettalk-server",
        "subnet-id",
        "subnet-mask",
        "subnet-selection",
        "subscriber-id",
        "swap-server",
        "t1-percent",
        "t2-percent",
        "tcp-keepalive-garbage",
        "tcp-keepalive-interval",
        "template-test",
        "tftp-server-name",
        "time-offset",
        "time-servers",
        "total-addresses",
        "total-nas",
        "trailer-encapsulation",
        "uap-servers",
        "unacked-clients",
        "unacked-clients-left",
        "user-class",
        "uuid-guid",
        "v4-captive-portal",
        "v4-lost",
        "v4-portparams",
        "v6-captive-portal",
        "valid-lft",
        "valid-lifetime",
        "vendor-class",
        "vendor-class-identifier",
        "vendor-encapsulated-options",
        "vendor-opts",
        "vivco-suboptions",
        "vivso-suboptions",
        "www-server",
        "x-display-manager",
    }
)

WIRE_FAMILY_KEYS = frozenset({"Dhcp4", "Dhcp6", "subnet4", "subnet6"})
_ARGUMENTS_KEYS = frozenset({"arguments"})
WIRE_LITERALS = WIRE_COMMANDS | WIRE_PAYLOAD_KEYS | WIRE_FAMILY_KEYS | _ARGUMENTS_KEYS
_WIRE_FSTRING = re.compile(
    r"(?:"
    r"(?:subnet|Dhcp|dhcp)\{\}"
    r"|option-(?:[a-z0-9-]|\{\})*\{\}(?:[a-z0-9-]|\{\})*"
    r")\Z"
)
# The part of a printf conversion after its mapping key, as CPython parses it.
_PRINTF_TAIL = re.compile(r"[-+ #0]*(?:\*|\d*)(?:\.(?:\*|\d*))?[hlL]?([diouxXeEfFgGcrsa%])")


@dataclass(frozen=True)
class Violation:
    """One wire literal outside its owning module."""

    path: str
    lineno: int
    qualname: str
    literal: str

    @property
    def site(self) -> str:
        return f"{self.path}::{self.qualname}"

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: unapproved Kea wire literal {self.literal!r} in {self.qualname}"


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.AST):
        self.rel = rel
        self.tree = tree
        self.scope: list[str] = []
        self.hits: list[Violation] = []
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def _record(self, node: ast.expr, literal: str) -> None:
        self.hits.append(Violation(self.rel, node.lineno, ".".join(self.scope) or "<module>", literal))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def _is_filter_key(self, node: ast.AST) -> bool:
        mapping = self.parents.get(node)
        if not isinstance(mapping, ast.Dict) or node not in mapping.keys:
            return False
        keyword = self.parents.get(mapping)
        if not isinstance(keyword, ast.keyword) or keyword.arg is not None:
            return False
        call = self.parents.get(keyword)
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"filter", "exclude"}
        )

    def _is_model_field(self, node: ast.expr) -> bool:
        if self._is_filter_key(node):
            return True
        assignment = self.parents.get(node)
        if not (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name)
        ):
            return False
        name = assignment.targets[0].id
        scope = self.parents.get(assignment, self.tree)
        while not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
            scope = self.parents.get(scope, self.tree)
        references = [n for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == name]
        stores = [n for n in references if isinstance(n.ctx, ast.Store)]
        reads = [n for n in references if isinstance(n.ctx, ast.Load)]
        return len(stores) == 1 and bool(reads) and all(self._is_filter_key(read) for read in reads)

    def _scan_template_tags(self, node: ast.expr, text: str) -> None:
        for tag in re.finditer(r"\{%.*?%\}|\{\{.*?\}\}", text, re.DOTALL):
            for quoted in re.finditer(r"""(["'])([a-zA-Z][a-zA-Z0-9-]*)\1""", tag.group()):
                if quoted[2] in WIRE_LITERALS - _ARGUMENTS_KEYS:
                    self._record(node, quoted[2])

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        self._scan_template_tags(node, node.value)
        self._check_literal(node, node.value)

    def _check_literal(self, node: ast.Constant, value: str) -> None:
        if value not in WIRE_LITERALS:
            return
        if value in _ARGUMENTS_KEYS:
            parent = self.parents.get(node)
            is_key = isinstance(parent, ast.Subscript) and parent.slice is node
            is_get = (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr == "get"
                and bool(parent.args)
                and parent.args[0] is node
            )
            is_helper = (
                isinstance(parent, ast.Call)
                and bool(parent.args)
                and isinstance(parent.args[0], ast.Name)
                and node in parent.args[1:]
            )
            if not (is_key or is_get or is_helper):
                return
        self._record(node, value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._visit_template(node)

    def visit_Call(self, node: ast.Call | ast.BinOp) -> None:
        if not self._visit_template(node):
            self.generic_visit(node)

    visit_BinOp = visit_Call

    def _visit_template(self, node: ast.expr) -> bool:
        """Check a string template once as a whole, then visit its holes; False when *node* is none."""
        template = _template(node)
        if template is None:
            return False
        self._scan_template_tags(node, template.shape)
        if not self._check_shape(node, template.shape):
            for text, value in template.texts:
                self._check_literal(text, value)
        for hole in template.holes:
            self.visit(hole)
        return True

    def _check_shape(self, node: ast.expr, shape: str) -> bool:
        """Record *node* when its text shape, with ``{}`` for each hole, is wire text."""
        is_field = shape == "dhcp{}" and self._is_model_field(node)
        # Match fixed command text against the same vocabulary as plain literals.
        # Require the complete family, so labels such as v{version} stay separate.
        family, separator, _ = shape.partition("-")
        has_family = bool(separator) and any(
            family.replace("{}", version) in _WIRE_COMMAND_FAMILIES for version in ("", "4", "6")
        )
        is_command = False
        if "{}" in shape and has_family:
            command_pattern = re.compile(re.escape(shape).replace(r"\{\}", "[a-z0-9-]*"))
            is_command = any(command_pattern.fullmatch(command) for command in WIRE_COMMANDS)
        if not is_field and (is_command or _WIRE_FSTRING.fullmatch(shape) or shape in WIRE_LITERALS - _ARGUMENTS_KEYS):
            self._record(node, ast.unparse(node))
            return True
        return False


def _format_shape(template: str) -> str | None:
    """Return a str.format template with {} for each field, or None when it cannot format."""
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        return None
    return "".join(literal + ("" if field is None else "{}") for literal, field, _, _ in fields)


def _printf_shape(template: str) -> str | None:
    """Return a % template with {} for each conversion, or None when it cannot format."""
    shape: list[str] = []
    index = 0
    while (start := template.find("%", index)) != -1:
        shape.append(template[index:start])
        index = start + 1
        if template.startswith("(", index):
            depth = 0
            while index < len(template):
                depth += {"(": 1, ")": -1}.get(template[index], 0)
                index += 1
                if depth == 0:
                    break
            if depth:
                return None
        conversion = _PRINTF_TAIL.match(template, index)
        if conversion is None:
            return None
        shape.append("%" if conversion[1] == "%" else "{}")
        index = conversion.end()
    shape.append(template[index:])
    return "".join(shape)


@dataclass(frozen=True)
class _Template:
    shape: str
    texts: tuple[tuple[ast.Constant, str], ...]
    holes: tuple[ast.AST, ...]


def _template(node: ast.AST) -> _Template | None:
    """Return a string template's text with {} for each hole, or None when *node* builds no string.

    f-strings, .format and % templates, and + chains of them are templates; any other operand is a hole.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _Template(node.value, ((node, node.value),), ())
    if isinstance(node, ast.JoinedStr):
        return _joined([_template(value) or _Template("{}", (), (value,)) for value in node.values])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        receiver = node.func.value
        if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
            shape = _format_shape(receiver.value)
            if shape is not None:
                return _Template(shape, ((receiver, receiver.value),), (*node.args, *node.keywords))
        return None
    if not isinstance(node, ast.BinOp):
        return None
    if isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        shape = _printf_shape(node.left.value)
        return None if shape is None else _Template(shape, ((node.left, node.left.value),), (node.right,))
    if isinstance(node.op, ast.Add):
        left, right = _template(node.left), _template(node.right)
        if left is None and right is None:
            return None
        return _joined([left or _Template("{}", (), (node.left,)), right or _Template("{}", (), (node.right,))])
    return None


def _joined(parts: list[_Template]) -> _Template:
    return _Template(
        "".join(part.shape for part in parts),
        tuple(text for part in parts for text in part.texts),
        tuple(hole for part in parts for hole in part.holes),
    )


def scan_source(src: str, rel: str = "<source>") -> list[Violation]:
    """Scan source text without applying tree ownership exclusions."""
    tree = ast.parse(src, filename=rel)
    scanner = _Scanner(rel, tree)
    scanner.visit(tree)
    return scanner.hits


def scan_tree(root: Path = PACKAGE_ROOT) -> list[Violation]:
    """Scan a package root, excluding exact owners, tests, and migrations."""
    hits: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.as_posix() in _OWNERS or rel.parts[0] in {"tests", "migrations"}:
            continue
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel.as_posix()))
    return hits


def load_baseline(path: Path = _BASELINE_PATH) -> dict[str, int]:
    """Read accepted per-file and per-scope literal counts."""
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        site, count = line.rsplit("\t", 1)
        budget = int(count)
        if not site or budget < 0 or site in counts:
            raise ValueError(f"Invalid baseline entry: {line!r}")
        counts[site] = budget
    return counts


def save_baseline(counts: dict[str, int], path: Path = _BASELINE_PATH) -> None:
    """Write sorted budgets; disappeared sites and reduced counts remain acceptable."""
    # REUSE-IgnoreStart
    header = [
        "# SPDX-FileCopyrightText: 2026 Marcin Zieba",
        "# SPDX-License-Identifier: Apache-2.0",
        "# Kea wire-discipline baseline is the follow-up brief for existing wire debt.",
        "# Entries only leave; counts only decrease. Use --update-baseline to record a decrease.",
        "# Each line: <relpath-from-netbox_kea>::<qualname>\\t<allowed-count>.",
        "# Budgets permit replacement within a scope; they do not identify exact literals.",
        "# Update: python3 netbox_kea/tests/kea_wire_discipline.py --update-baseline",
        "",
    ]
    # REUSE-IgnoreEnd
    body = [f"{site}\t{count}" for site, count in sorted(counts.items())]
    path.write_text("\n".join(header + body).rstrip("\n") + "\n", encoding="utf-8")


def unapproved(root: Path = PACKAGE_ROOT, baseline: dict[str, int] | None = None) -> list[Violation]:
    """Return only sites whose counts exceed their accepted budgets."""
    allowed = load_baseline() if baseline is None else baseline
    by_site: dict[str, list[Violation]] = {}
    for hit in scan_tree(root):
        by_site.setdefault(hit.site, []).append(hit)
    extra = [
        hit
        for site, hits in by_site.items()
        for hit in sorted(hits, key=lambda hit: hit.lineno)[allowed.get(site, 0) :]
    ]
    return sorted(extra, key=lambda hit: (hit.path, hit.lineno))


def _main(argv: list[str], *, root: Path = PACKAGE_ROOT, baseline_path: Path = _BASELINE_PATH) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)
    if args.update_baseline:
        counts = dict(Counter(hit.site for hit in scan_tree(root)))
        allowed = load_baseline(baseline_path)
        increases = {site: count for site, count in counts.items() if count > allowed.get(site, 0)}
        if increases:
            for site, count in sorted(increases.items()):
                print(f"{site}: baseline budget would increase from {allowed.get(site, 0)} to {count}")
            print("baseline unchanged: fix new wire literals before recording decreases")
            return 1
        save_baseline(counts, baseline_path)
        print(f"baseline updated: {sum(counts.values())} literal(s) across {len(counts)} site(s)")
        return 0
    bad = unapproved(root, load_baseline(baseline_path))
    for hit in bad:
        print(hit)
    print(f"{len(bad)} unapproved Kea wire literal(s)")
    return int(bool(bad))


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
