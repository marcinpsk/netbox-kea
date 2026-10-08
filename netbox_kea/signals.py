# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Django signals emitted by the netbox-kea plugin for lease and reservation events.

External consumers can connect to these signals to react to DHCP changes::

    from netbox_kea.signals import lease_added, leases_deleted

    def on_lease_added(sender, server, creation, lease, dhcp_version, request, **kwargs):
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
    Fired when Kea confirms a lease that the plugin UI added.
    kwargs: ``server``, ``creation`` (the ``LeaseRequest`` that Kea confirmed),
    ``lease`` (the ``Lease`` that a fresh read observed after the creation, or ``None``
    when that read failed, found no lease, found a malformed one, or found one whose client
    binding, Subnet or hostname differs from the request), ``dhcp_version``, ``request``

leases_deleted
    Fired when Kea confirms the deletion of one or more leases selected in the plugin UI.
    kwargs: ``server``, ``leases`` (a tuple of the ``Lease`` values that the fresh read before
    each deletion observed; a delegated prefix is a ``DHCPv6PrefixLease`` with its prefix length),
    ``dhcp_version``, ``request``. A lease that changed after the list was shown, or that Kea
    no longer holds, is not deleted and is not in ``leases``.

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

The ``creation``, ``lease``, ``leases``, ``before`` and ``after`` values use the immutable typed
Lease and Reservation domains (``netbox_kea.leases`` and ``netbox_kea.reservations``). They do not
depend on the route or Kea's raw response shape.
"""

from django.dispatch import Signal

lease_added = Signal()
leases_deleted = Signal()
reservation_created = Signal()
reservation_updated = Signal()
reservation_deleted = Signal()
