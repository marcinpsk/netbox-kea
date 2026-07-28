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
    kwargs: ``server``, ``reservation`` (dict), ``dhcp_version``, ``request``

reservation_updated
    Fired when a host reservation is updated via the plugin UI.
    kwargs: ``server``, ``reservation`` (dict), ``dhcp_version``, ``request``

reservation_deleted
    Fired when a host reservation is deleted via the plugin UI.
    kwargs: ``server``, ``ip_address``, ``identifier_type``, ``identifier``,
    ``dhcp_version``, ``request``

    A reservation can be addressed either by its IP or, when it reserves no
    address, by its client identifier. All three keys are sent on every delete
    regardless of which route was used, each ``None`` where it does not apply, so
    receivers never have to handle a route-dependent payload shape.
"""

from django.dispatch import Signal

lease_added = Signal()
leases_deleted = Signal()
reservation_created = Signal()
reservation_updated = Signal()
reservation_deleted = Signal()
