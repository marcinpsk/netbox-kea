# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Django signals emitted by the netbox-kea plugin for lease and reservation events.

External consumers can connect to these signals to react to DHCP changes::

    from netbox_kea.signals import lease_added, leases_deleted

    def on_lease_added(sender, server, ip_address, hw_address, hostname, dhcp_version, request, **kwargs):
        # your logic here
        ...

    lease_added.connect(on_lease_added)

All signals are fired *after* the Kea API call succeeds, so receivers can
safely assume the change is in effect. They are *not* fired when the call fails.
They *are* fired when the change was applied but Kea could not write it to disk
(``config-write`` failed) — it is live now and may not survive a restart.

Signals
-------
lease_added
    Fired when a single DHCP lease is added via the plugin UI.
    kwargs: ``server``, ``ip_address``, ``hw_address``, ``hostname``,
    ``dhcp_version``, ``request``

leases_deleted
    Fired when one or more DHCP leases are deleted via the plugin UI.
    kwargs: ``server``, ``ip_addresses`` (list[str]), ``dhcp_version``, ``request``

reservation_created
    Fired when a host reservation is created via the plugin UI.
    kwargs: ``server``, ``before`` (None), ``after`` (Reservation),
    ``dhcp_version``, ``request``

reservation_updated
    Fired when a host reservation is updated via the plugin UI.
    kwargs: ``server``, ``before`` (Reservation), ``after`` (Reservation),
    ``dhcp_version``, ``request``

reservation_deleted
    Fired when a host reservation is deleted via the plugin UI.
    kwargs: ``server``, ``before`` (Reservation), ``after`` (None),
    ``dhcp_version``, ``request``

The ``before`` and ``after`` values use the immutable typed Reservation domain.
They do not depend on the route or Kea's raw response shape.

With netbox-branching installed, ``pre_save`` and ``pre_delete`` receivers refuse a change of
a netbox_kea row while a branch is active (ADR 0007).
"""

from typing import Any

from django.apps import apps
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import Signal

from .branching import APP_LABEL, refuse_in_branch

lease_added = Signal()
leases_deleted = Signal()
reservation_created = Signal()
reservation_updated = Signal()
reservation_deleted = Signal()


def _refuse_save_in_branch(sender: Any, instance: Any, **kwargs: Any) -> None:
    refuse_in_branch(f"A save of {sender._meta.label} {instance.pk}")


def _refuse_delete_in_branch(sender: Any, instance: Any, **kwargs: Any) -> None:
    refuse_in_branch(f"A delete of {sender._meta.label} {instance.pk}")


def connect_branch_refusal() -> None:
    """Refuse a save or a delete of every netbox_kea row in a branch: the resolver keeps each model in main.

    A pre_delete receiver disables Django's fast delete, so a queryset delete() reaches it too.
    """
    for model in apps.get_app_config(APP_LABEL).get_models():
        uid = f"{APP_LABEL}.refuse_in_branch.{model._meta.label}"
        pre_save.connect(_refuse_save_in_branch, sender=model, dispatch_uid=uid)
        pre_delete.connect(_refuse_delete_in_branch, sender=model, dispatch_uid=uid)
