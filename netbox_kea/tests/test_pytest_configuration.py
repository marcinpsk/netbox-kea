# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for pytest configuration shared by unit and integration suites."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def test_pytest_configuration_works_with_django_plugin_disabled():
    """Keep unit-only pytest options out of the integration test command."""
    repository_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=repository_root) as temporary_directory:
        placeholder_test = Path(temporary_directory) / "test_placeholder.py"
        placeholder_test.write_text("def test_placeholder():\n    pass\n")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(placeholder_test), "--collect-only", "-q", "-p", "no:django"],
            cwd=repository_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, result.stdout + result.stderr
