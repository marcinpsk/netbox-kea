# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-config-change-mapper-outside-base. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
# The rule excludes views/_base.py by path, which `opengrep test` does not apply, so every mapper here is flagged.
from netbox_kea.config_write import ConfigChangeRejected


# ruleid: kea-config-change-mapper-outside-base
def _run_config_change(request, confirmed, change):
    try:
        outcome = change()
    # ok: kea-config-change-rejection-caught-outside-mapper
    except ConfigChangeRejected as rejection:
        messages.error(request, rejection.reason)
        return None
    return outcome


# ok: kea-config-change-mapper-outside-base
def _run_subnet_change(request, change):
    return _run_config_change(request, "Saved.", change)
