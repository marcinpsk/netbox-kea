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


# Every other on_delete handler writes the referencing row: CASCADE, SET_NULL, SET_DEFAULT, SET(...), DB_*.
_NON_WRITING_ON_DELETE = (models.PROTECT, models.RESTRICT, models.DO_NOTHING)


def is_branchable(model: type[models.Model]) -> bool | None:
    """Resolve branching support for a plugin model, and defer (None) for every other model.

    A plugin model is branchable when a concrete foreign key of it writes on delete to a branchable
    model outside the plugin: a delete of that model in a branch then writes the plugin table, which
    must exist in the branch schema. netbox-branching also calls this with historical models.
    """
    if model._meta.app_label != APP_LABEL:
        return None
    from netbox_branching.utilities import supports_branching

    return any(
        supports_branching(field.related_model)
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey)
        and field.related_model._meta.app_label != APP_LABEL
        and field.remote_field.on_delete not in _NON_WRITING_ON_DELETE
    )


def register() -> None:
    """Register the resolver with netbox-branching, from the plugin's ready()."""
    if not installed():
        return
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(is_branchable)
