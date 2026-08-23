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
