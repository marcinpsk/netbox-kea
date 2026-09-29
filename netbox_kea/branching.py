# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one owner of every netbox-branching fact the plugin relies on (ADR 0007).

netbox-branching is optional. Without it, every function here is a no-op. This is the only
module in the plugin that imports ``netbox_branching``.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import models

APP_LABEL = "netbox_kea"
BRANCHING_APP_LABEL = "netbox_branching"


def installed() -> bool:
    """Return whether netbox-branching is an installed app."""
    return apps.is_installed(BRANCHING_APP_LABEL)


def active_branch() -> Any:
    """Return the active Branch, or None when no branch is active or netbox-branching is absent."""
    if not installed():
        return None
    from netbox_branching.contextvars import active_branch as branch_context

    return branch_context.get()


def is_branchable(model: type[models.Model]) -> bool | None:
    """Keep every netbox_kea model in main (False), and defer (None) for every other model.

    A constant, so it cannot raise: netbox-branching treats a raising resolver as no answer and then
    makes a change-logged model such as Server branchable. Guard 2 in test_branching.py computes the
    foreign-key rule and fails when a plugin model would need a branch copy. netbox-branching also
    calls this with historical models.
    """
    return False if model._meta.app_label == APP_LABEL else None


def register() -> None:
    """Register the resolver with netbox-branching, from the plugin's ready()."""
    if not installed():
        return
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(is_branchable)
