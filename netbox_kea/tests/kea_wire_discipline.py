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
Quoted wire literals in Django template tags count. Proven filter/exclude keyword
names are model fields, including service-shaped f-strings used only as those names.
"""

from __future__ import annotations

import argparse
import ast
import re
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
_FORMAT_PLACEHOLDER = re.compile(r"\{[^{}]*\}|%(?:\([^)]*\))?[sdi]")


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
        if node.value not in WIRE_LITERALS:
            return
        if node.value in _ARGUMENTS_KEYS:
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
        self._record(node, node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        shape = _shape(node)
        self._scan_template_tags(node, shape)
        self._check_shape(node, shape)
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                self.visit(value)

    def visit_Call(self, node: ast.Call) -> None:
        template = (
            _text(node.func.value) if isinstance(node.func, ast.Attribute) and node.func.attr == "format" else None
        )
        if template is not None and self._check_shape(node, _FORMAT_PLACEHOLDER.sub("{}", template)):
            for child in [*node.args, *node.keywords]:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        parent = self.parents.get(node)
        if isinstance(node.op, ast.Mod) and (template := _text(node.left)) is not None:
            if self._check_shape(node, _FORMAT_PLACEHOLDER.sub("{}", template)):
                self.visit(node.right)
                return
        elif isinstance(node.op, ast.Add) and not (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add)):
            parts = _concatenation(node)
            has_text = any(_text(part) is not None or isinstance(part, ast.JoinedStr) for part in parts)
            if has_text and self._check_shape(node, "".join(_shape(part) for part in parts)):
                for part in parts:
                    holes = part.values if isinstance(part, ast.JoinedStr) else [part]
                    for hole in holes:
                        if _text(hole) is None:
                            self.visit(hole)
                return
        self.generic_visit(node)

    def _check_shape(self, node: ast.expr, shape: str) -> bool:
        """Record *node* when its text shape, with ``{}`` for each hole, is wire text."""
        is_field = shape == "dhcp{}" and self._is_model_field(node)
        # Match fixed command text against the same vocabulary as plain literals.
        # Require the complete family, so labels such as v{version} stay separate.
        family, separator, _ = shape.partition("-")
        has_family = bool(separator) and any(
            family.replace("{}", str(version)) in _WIRE_COMMAND_FAMILIES for version in (4, 6)
        )
        is_command = False
        if "{}" in shape and has_family:
            command_pattern = re.compile(re.escape(shape).replace(r"\{\}", "[a-z0-9-]+"))
            is_command = any(command_pattern.fullmatch(command) for command in WIRE_COMMANDS)
        if not is_field and (is_command or _WIRE_FSTRING.fullmatch(shape) or shape in WIRE_LITERALS - _ARGUMENTS_KEYS):
            self._record(node, ast.unparse(node))
            return True
        return False


def _text(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _shape(node: ast.AST) -> str:
    """Return string text with ``{}`` for each hole; any other expression is one hole."""
    text = _text(node)
    if text is not None:
        return text
    if isinstance(node, ast.JoinedStr):
        return "".join(_shape(value) for value in node.values)
    return "{}"


def _concatenation(node: ast.expr) -> list[ast.expr]:
    """Return the operands of a left-nested ``+`` chain in source order."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_concatenation(node.left), *_concatenation(node.right)]
    return [node]


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
