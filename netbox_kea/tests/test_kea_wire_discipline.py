# SPDX-FileCopyrightText: 2026 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0
"""Exercise the wire-literal guard and enforce it on production code."""

import ast
import re
from pathlib import Path

import pytest

from netbox_kea.tests import kea_wire_discipline as wd

# This is the frozen follow-up brief. Files may leave but must not be added.
_FOLLOW_UP_FILES = frozenset(
    {
        "constants.py",
        "integrations/dhcp_plugin.py",
        "mappers/kea_to_dhcp.py",
        "models.py",
        "reservation_transfer.py",
        "sync.py",
        "tables.py",
        "utilities.py",
        "views/_base.py",
        "views/dhcp_control.py",
        "views/dhcp_plugin_sync.py",
        "views/leases.py",
        "views/options.py",
        "views/reservation_mutations.py",
        "views/reservations.py",
        "views/server.py",
    }
)

# Explicit domain diagnostics, never Kea payload keys or commands.
_NON_WIRE_HYPHENATED = {
    "ambiguous-identifier": "Reservation validation diagnostic",
    "catalogue-identity-collision": "Catalogue validation diagnostic",
    "configuration-changed-during-retry": "Catalogue retry diagnostic",
    "configuration-unavailable": "Configuration snapshot diagnostic",
    "duplicate-address": "Transfer validation diagnostic",
    "duplicate-option": "Transfer validation diagnostic",
    "duplicate-prefix": "Transfer validation diagnostic",
    "duplicate-reservation": "Transfer validation diagnostic",
    "duplicate-subnet-id": "Configuration validation diagnostic",
    "identity-command-unavailable": "Catalogue availability diagnostic",
    "identity-configuration-disagreement": "Catalogue validation diagnostic",
    "identity-unavailable": "Catalogue availability diagnostic",
    "invalid-address": "Reservation validation diagnostic",
    "invalid-addresses": "Reservation validation diagnostic",
    "invalid-document": "Transfer validation diagnostic",
    "invalid-family": "Transfer validation diagnostic",
    "invalid-family-identifier": "Reservation validation diagnostic",
    "invalid-hostname": "Reservation validation diagnostic",
    "invalid-identifier": "Reservation validation diagnostic",
    "invalid-identity": "Transfer validation diagnostic",
    "invalid-option": "Option validation diagnostic",
    "invalid-option-collection": "Configuration validation diagnostic",
    "invalid-option-definition": "Configuration validation diagnostic",
    "invalid-option-definition-collection": "Configuration validation diagnostic",
    "invalid-options": "Reservation validation diagnostic",
    "invalid-pool": "Configuration validation diagnostic",
    "invalid-pool-collection": "Configuration validation diagnostic",
    "invalid-prefix": "Reservation validation diagnostic",
    "invalid-prefixes": "Reservation validation diagnostic",
    "invalid-record": "Reservation validation diagnostic",
    "invalid-reservations": "Transfer validation diagnostic",
    "invalid-scope": "Reservation validation diagnostic",
    "invalid-setting": "Configuration validation diagnostic",
    "invalid-shared-network": "Configuration validation diagnostic",
    "invalid-shared-network-collection": "Configuration validation diagnostic",
    "invalid-shared-network-membership": "Catalogue validation diagnostic",
    "invalid-subnet": "Configuration validation diagnostic",
    "invalid-subnet-cidr": "Configuration validation diagnostic",
    "invalid-subnet-collection": "Configuration validation diagnostic",
    "invalid-subnet-id": "Configuration validation diagnostic",
    "invalid-version": "Transfer validation diagnostic",
    "malformed-configuration-response": "Configuration snapshot diagnostic",
    "malformed-identity-response": "Catalogue snapshot diagnostic",
    "missing-identifier": "Reservation validation diagnostic",
    "out-of-subnet-address": "Transfer validation diagnostic",
    "page-fetch-failed": "Reservation snapshot diagnostic",
    "page-limit-reached": "Reservation snapshot diagnostic",
    "pagination-stalled": "Reservation snapshot diagnostic",
    "state-command": "Lease preflight diagnostic",
    "target-mismatch": "Reservation mutation diagnostic",
    "unknown-field": "Transfer validation diagnostic",
    "unsupported-identifier": "Reservation validation diagnostic",
    "unsupported-scope": "Transfer validation diagnostic",
    "unverified-scope": "Reservation validation diagnostic",
    "wrong-family": "Transfer validation diagnostic",
    "in-subnet": "Reservation Scope enum value",
    "not-applicable": "Synchronization State enum value",
    "not-requested": "Synchronization State enum value",
    "not-synchronized": "Synchronization State enum value",
    "partially-synchronized": "Synchronization State enum value",
    "font-monospace": "Bootstrap CSS class",
    "text-break": "Bootstrap CSS class",
    "text-danger": "Bootstrap CSS class",
    "text-warning": "Bootstrap CSS class",
    "kea-reservation-subnet-cidrs": "NetBox custom-field name",
    "utf-8": "Transfer document encoding",
    "stat-lease": "Static fragment of a family-specific command f-string",
}


def test_baseline_files_only_leave_the_follow_up_brief():
    assert {site.split("::", 1)[0] for site in wd.load_baseline()} <= _FOLLOW_UP_FILES


def test_owner_and_follow_up_hyphenated_literals_have_guard_coverage():
    missing = []
    for rel in sorted(wd._OWNERS | _FOLLOW_UP_FILES):
        module = ast.parse((wd.PACKAGE_ROOT / rel).read_text())
        for node in ast.walk(module):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if (
                re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)+", node.value)
                and node.value not in _NON_WIRE_HYPHENATED
                and not wd.scan_source(f"value = {node.value!r}")
            ):
                missing.append(f"{rel}:{node.lineno}: {node.value}")
    assert not missing, "\n".join(missing)


def test_direct_family_command_templates_have_concrete_vocabulary_entries():
    module = ast.parse((wd.PACKAGE_ROOT / "kea.py").read_text())
    commands = set()
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"command", "_config_mutation_command"}
            and node.args
        ):
            continue
        command = node.args[0]
        if isinstance(command, ast.Constant) and isinstance(command.value, str):
            commands.add(command.value)
        elif isinstance(command, ast.JoinedStr):
            shape = "".join(
                value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "{}"
                for value in command.values
            )
            if shape.count("{}") == 1:
                commands.update(shape.format(family) for family in (4, 6))
    assert commands <= wd.WIRE_COMMANDS


def test_production_literal_has_source_location_and_scope():
    hits = wd.scan_source(
        'class View:\n    async def read(self):\n        return row["option-data"]\n', "views/options.py"
    )
    assert len(hits) == 1
    assert hits[0].site == "views/options.py::View.read"
    assert hits[0].lineno == 3
    assert "option-data" in str(hits[0])


@pytest.mark.parametrize(
    "literal",
    [
        "config-get",
        "hw-address",
        "total-addresses",
        "total-nas",
        "ha-heartbeat",
        "statistic-get-all",
        "network4-list",
        "network6-list",
        "pd-pools",
        "shared-networks",
        "lease4-get",
        "subnet6-list",
        "status-get",
        "version-get",
        "lease4-del",
        "lease6-del",
    ],
)
def test_exact_wire_literals_are_flagged(literal):
    assert len(wd.scan_source(f"value = {literal!r}", "views/server.py")) == 1


@pytest.mark.parametrize(
    "key",
    [
        "high-availability",
        "ha-servers",
        "ha-mode",
        "last-state",
        "unacked-clients",
        "next-server",
        "boot-file-name",
        "ddns-send-updates",
        "reservations-global",
        "template-test",
        "only-in-additional-list",
    ],
)
def test_existing_wire_payload_fields_are_flagged(key):
    assert len(wd.scan_source(f"value = payload[{key!r}]")) == 1


def test_all_registered_kea_settings_have_guard_coverage():
    path = Path(__file__).parents[1] / "mappers" / "kea_to_dhcp.py"
    module = ast.parse(path.read_text())
    declaration = next(
        node
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "KEA_SETTINGS_KEYS"
    )
    keys = ast.literal_eval(declaration.value)
    assert all(wd.scan_source(f"value = payload[{key!r}]") for key in keys if "-" in key)


@pytest.mark.parametrize(
    "expression",
    [
        'f"config-{operation}"',
        'f"lease4-{operation}"',
        'f"subnet6-{operation}"',
        'f"reservation-get-{selector}"',
        'f"option-def-{operation}"',
        'f"stat-lease4-{operation}"',
    ],
)
def test_wire_fstrings_allow_fixed_command_segments(expression):
    assert len(wd.scan_source(f"value = {expression}")) == 1


@pytest.mark.parametrize("key", ["subnet", "pool", "pools", "name", "state", "identifier", "description", "interface"])
def test_form_fields_are_not_wire_literals(key):
    assert wd.scan_source(f"value = cleaned_data[{key!r}]", "forms.py") == []


@pytest.mark.parametrize(
    "key", ["dhcp4", "dhcp6", "result", "text", "command", "service", "relay", "duid", "iaid", "cltt"]
)
def test_ambiguous_words_are_not_wire_literals(key):
    assert wd.scan_source(f"field = {key!r}\nvalue = payload[{key!r}]", "models.py") == []


@pytest.mark.parametrize(
    "expression", ['payload["arguments"]', 'payload.get("arguments")', '_read(payload, "arguments")']
)
def test_arguments_reads_are_flagged(expression):
    assert len(wd.scan_source(f"value = {expression}", "views/server.py")) == 1


@pytest.mark.parametrize(
    "expression",
    [
        '"arguments"',
        '{"arguments": 1}',
        'row.get("display", "arguments")',
        '_read("arguments")',
        '_read(factory(), "arguments")',
        '_read(payload, key="arguments")',
    ],
)
def test_arguments_outside_read_positions_are_ignored(expression):
    assert wd.scan_source(f"value = {expression}") == []


@pytest.mark.parametrize("method", ["filter", "exclude"])
def test_service_shaped_model_filter_field_is_ignored(method):
    source = (
        "def _get_servers(request, dhcp_version):\n"
        '    dhcp_kwarg = f"dhcp{dhcp_version}"\n'
        '    selected_pks = request.GET.getlist("server")\n'
        f'    return Server.objects.restrict(request.user, "view").{method}(**{{dhcp_kwarg: True}})\n'
    )
    assert wd.scan_source(source, "views/combined.py") == []
    assert wd.scan_source(f'Server.objects.{method}(**{{f"dhcp{{version}}": True}})') == []


def test_model_filter_does_not_hide_service_use_of_the_same_value():
    source = (
        'service = f"dhcp{version}"\n'
        "Server.objects.filter(**{service: True})\n"
        'client.command("status-get", service=[service])\n'
    )
    assert len(wd.scan_source(source)) == 2


def test_django_template_wire_comparison_is_flagged():
    source = '''template = """<span>{% if record.identifier_type == "hw-address" %}MAC{% endif %}</span>"""'''
    hits = wd.scan_source(source, "tables.py")
    assert [hit.literal for hit in hits] == ["hw-address"]
    assert wd.scan_source("template = '<span class=\"hw-address\">MAC</span>'") == []


@pytest.mark.parametrize(
    "expression",
    [
        'f"subnet{version}"',
        'f"reservation-{operation}"',
        'f"stat-lease{version}-get"',
        'f"Dhcp{version}"',
        'f"dhcp{version}"',
        'f"network{version}-list"',
        'f"lease{version}-get"',
    ],
)
def test_wire_fstrings_are_flagged_once(expression):
    assert len(wd.scan_source(f"value = {expression}", "views/server.py")) == 1


@pytest.mark.parametrize(
    "expression",
    [
        'f"reservation_cursor_{pk}"',
        'f"reservations[{index}]"',
        'f"subnet {id}"',
        'f"subnet{version} returned no data"',
        '"config-get failed"',
        'f"DHCPv{v}"',
    ],
)
def test_presentation_strings_are_ignored(expression):
    assert wd.scan_source(f"value = {expression}") == []


def test_fstring_expressions_are_still_scanned():
    assert len(wd.scan_source("value = f\"Label: {row['arguments']}\"")) == 1


def test_documented_wire_owners_match_tree_exclusions(tmp_path):
    guidance = (wd.PACKAGE_ROOT.parent / "AGENTS.md").read_text()
    section = guidance.split("**Kea wire-discipline gate.**", 1)[1].split("\n- **", 1)[0]
    documented = set(re.findall(r"`([a-z_]+(?:/[a-z_]+)*\.py)`", section))
    documented.discard("netbox_kea/tests/kea_wire_discipline.py")
    assert documented == wd._OWNERS | {"tests/kea_stub.py"}
    for rel in documented | {"views/kea_stub.py", "views/kea.py"}:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('value = "config-get"\n')
    assert [hit.path for hit in wd.scan_tree(tmp_path)] == ["views/kea.py", "views/kea_stub.py"]


def test_tree_excludes_exact_owners_tests_and_migrations(tmp_path):
    paths = [
        "kea.py",
        "server_configuration.py",
        "subnet_catalogue.py",
        "reservations.py",
        "dhcp_options.py",
        "tests/kea_stub.py",
        "migrations/0001_initial.py",
        "views/reservations.py",
        "views/kea.py",
    ]
    for rel in paths:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('value = "config-get"\n')
    assert [hit.path for hit in wd.scan_tree(tmp_path)] == ["views/kea.py", "views/reservations.py"]


def test_baseline_budgets_allow_decreases_and_reject_increases(tmp_path):
    (tmp_path / "consumer.py").write_text('def read():\n    return "config-get", "option-data"\n')
    assert len(wd.unapproved(tmp_path, {"consumer.py::read": 1})) == 1
    assert wd.unapproved(tmp_path, {"consumer.py::read": 2}) == []
    assert wd.unapproved(tmp_path, {"consumer.py::read": 3, "gone.py::old": 9}) == []


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.txt"
    assert wd.load_baseline(path) == {}
    counts = {"b.py::read": 2, "a.py::<module>": 1}
    wd.save_baseline(counts, path)
    assert wd.load_baseline(path) == counts
    assert not path.read_text().endswith("\n\n")


@pytest.mark.parametrize(
    "existing",
    [{}, {"consumer.py::<module>": 1}, {"consumer.py::other": 2}],
    ids=["new-file", "increased-count", "new-scope"],
)
def test_cli_refuses_baseline_growth_without_writing(tmp_path, capsys, existing):
    (tmp_path / "consumer.py").write_text('value = "config-get", "option-data"\n')
    baseline = tmp_path / "baseline.txt"
    wd.save_baseline(existing, baseline)
    original = baseline.read_bytes()
    assert wd._main(["--update-baseline"], root=tmp_path, baseline_path=baseline) == 1
    assert "consumer.py::<module>" in capsys.readouterr().out
    assert baseline.read_bytes() == original


def test_cli_does_not_bootstrap_missing_baseline(tmp_path):
    (tmp_path / "consumer.py").write_text('value = "config-get"\n')
    baseline = tmp_path / "baseline.txt"
    assert wd._main(["--update-baseline"], root=tmp_path, baseline_path=baseline) == 1
    assert not baseline.exists()


def test_cli_reports_debt_and_records_only_decreases(tmp_path, capsys):
    (tmp_path / "consumer.py").write_text('value = "config-get", "option-data"\n')
    baseline = tmp_path / "baseline.txt"
    wd.save_baseline({"consumer.py::<module>": 1, "gone.py::old": 9}, baseline)
    assert wd._main([], root=tmp_path, baseline_path=baseline) == 1
    assert "consumer.py:1" in capsys.readouterr().out
    (tmp_path / "consumer.py").write_text('value = "config-get"\n')
    assert wd._main(["--update-baseline"], root=tmp_path, baseline_path=baseline) == 0
    assert wd.load_baseline(baseline) == {"consumer.py::<module>": 1}
    assert wd._main([], root=tmp_path, baseline_path=baseline) == 0
    (tmp_path / "consumer.py").write_text('value = "display"\n')
    assert wd._main(["--update-baseline"], root=tmp_path, baseline_path=baseline) == 0
    assert wd.load_baseline(baseline) == {}


def test_nested_scopes_have_separate_budgets():
    source = (
        'value = "hw-address"\ndef read():\n    def inner():\n        return "option-data"\n    return "config-get"\n'
    )
    assert [hit.qualname for hit in wd.scan_source(source)] == ["<module>", "read.inner", "read"]


@pytest.mark.parametrize(
    "entry", ["consumer.py::read\t-1\n", "consumer.py::read\tnan\n", "consumer.py::read\t1\nconsumer.py::read\t2\n"]
)
def test_invalid_baseline_fails_loudly(tmp_path, entry):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(entry)
    with pytest.raises(ValueError):
        wd.load_baseline(baseline)


def test_no_unapproved_wire_literals_in_production_tree():
    bad = wd.unapproved()
    assert not bad, "\n".join(str(hit) for hit in bad)
