# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The permission rule of a manual Sync.

A manual Sync makes the writes of the automatic sync, and no permission constraint limits the automatic sync.
So a user may run a manual Sync only with an unconstrained grant of each permission that its writes need.
The writer names those permissions: see ``claim_permissions`` and ``reconcile_permissions`` in ``ipam_reconciliation``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import Q
from users.models import ObjectPermission


@dataclass(frozen=True)
class SyncGate:
    """The result of the manual Sync rule for one user and one write path."""

    missing: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        """Return whether the user may run the manual Sync."""
        return not self.missing

    @property
    def reason(self) -> str:
        """Return why the user may not run the manual Sync, or an empty string when the user may."""
        if not self.missing:
            return ""
        noun = "permission" if len(self.missing) == 1 else "permissions"
        return (
            f"Manual Sync needs unconstrained {', '.join(self.missing)} {noun}: "
            "the automatic sync is not limited by permission constraints."
        )


def sync_gate(user: Any, permissions: Iterable[str]) -> SyncGate:
    """Apply the manual Sync rule to *user* for each of *permissions* (``app_label.action_model``)."""
    required = tuple(dict.fromkeys(permissions))
    if user.is_active and user.is_superuser:
        return SyncGate(())
    if not user.is_active:
        return SyncGate(required)
    unconstrained = _unconstrained(user, set(required))
    return SyncGate(tuple(name for name in required if name not in unconstrained))


def _unconstrained(user: Any, names: set[str]) -> set[str]:
    """Return each permission of *names* that NetBox grants to *user* for all objects of its model.

    This reads the grants as NetBox's ``ObjectPermissionBackend`` does: ``DEFAULT_PERMISSIONS`` and each enabled
    ObjectPermission of the user or of a group of the user. The constraint sets of all grants of one permission join.
    """
    constraints: dict[str, list[Any]] = {}
    for name in names & settings.DEFAULT_PERMISSIONS.keys():
        constraints.setdefault(name, []).extend(settings.DEFAULT_PERMISSIONS[name] or ())
    grants = (
        ObjectPermission.objects.filter(Q(users=user) | Q(groups__user=user), enabled=True)
        .order_by("pk")
        .distinct("pk")
        .prefetch_related("object_types")
    )
    for grant in grants:
        for object_type in grant.object_types.all():
            for action in grant.actions:
                name = f"{object_type.app_label}.{action}_{object_type.model}"
                if name in names:
                    constraints.setdefault(name, []).extend(grant.list_constraints())
    # NetBox reads no constraint set, or one empty constraint set, as access to all objects.
    return {name for name, sets in constraints.items() if not sets or not all(sets)}
