# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for subprocess-without-timeout. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
import subprocess
from subprocess import check_output, run


def bad_run(command):
    # ruleid: subprocess-without-timeout
    return subprocess.run(command, capture_output=True, check=False)


def bad_call(command):
    # ruleid: subprocess-without-timeout
    return subprocess.call(command)


def bad_check_call(command):
    # ruleid: subprocess-without-timeout
    subprocess.check_call(command)


def bad_check_output(command):
    # ruleid: subprocess-without-timeout
    return subprocess.check_output(command, text=True)


def bad_imported_name(command):
    # ruleid: subprocess-without-timeout
    return run(command)


def bad_imported_check_output(command):
    # ruleid: subprocess-without-timeout
    return check_output(command)


def good_run(command):
    # ok: subprocess-without-timeout
    return subprocess.run(command, capture_output=True, check=False, timeout=30)


def good_timeout_first(command):
    # ok: subprocess-without-timeout
    return subprocess.check_output(command, timeout=5, text=True)


def good_imported_name(command, seconds):
    # ok: subprocess-without-timeout
    return run(command, timeout=seconds)


def good_popen_wait(command):
    process = subprocess.Popen(command)
    # ok: subprocess-without-timeout
    return process.wait(timeout=30)
