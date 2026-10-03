# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the repository's domain-language documentation."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_context_defines_the_dhcp_option_term_consistently():
    """Keep the Catalogue glossary on its canonical singular domain term."""
    context = (REPOSITORY_ROOT / "CONTEXT.md").read_text()

    assert context.count("**DHCP Option**:") == 1
    assert "DHCP Options" not in context


def test_branching_decisions_keep_ownership_links_in_main():
    """The decisions must agree with the ownership link's main-only routing."""
    design = (REPOSITORY_ROOT / "docs/design/netbox-branching.md").read_text()
    decisions = design.split("## Decisions\n", 1)[1].split("## Design\n", 1)[0]
    opening, table = decisions.split("| # |", 1)
    assert "ADR 0006 link model main-only" in " ".join(opening.split())
    exemption_decision = next(row for row in table.splitlines() if row.startswith("| D14 |"))
    assert "first branchable plugin model" not in exemption_decision
    assert "ADR 0006" in exemption_decision
    assert "main-only" in exemption_decision


def test_agent_sync_lifecycle_uses_the_current_ownership_interfaces():
    """Direct agents to the phase reconciler and the claim-only entry point."""
    instructions = (REPOSITORY_ROOT / "AGENTS.md").read_text()
    lifecycle = instructions.split("**Sync lifecycle**:", 1)[1].split("\n- **", 1)[0]

    assert "reconcile()" in lifecycle
    assert "claim()" in lifecycle
    for retired in ("cleanup_stale_ips_batch()", "_sync()", "cleanup=False"):
        assert retired not in lifecycle
