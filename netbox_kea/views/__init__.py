# SPDX-FileCopyrightText: 2025 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0
"""Views package for netbox_kea.

Importing every submodule here is what makes their ``register_model_view``
decorators run, so NetBox knows the Server tabs before ``urls.py`` asks
``get_model_urls`` for them.
"""

from . import (
    _base,
    combined,
    dhcp_control,
    dhcp_plugin_sync,
    leases,
    options,
    reservation_mutations,
    reservations,
    server,
    shared_networks,
    subnets,
    sync_jobs,
    sync_views,
)
