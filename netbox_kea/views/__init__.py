# SPDX-FileCopyrightText: 2025 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0
"""Views package for netbox_kea — re-exports everything for backward-compatible imports."""

# Base helpers (ConditionalLoginRequiredMixin try/except lives here)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin, _strip_empty_params  # noqa: F401

# Combined / global cross-server views + data-fetch helpers
from .combined import (  # noqa: F401
    CombinedDashboardView,
    CombinedLeases4View,
    CombinedLeases6View,
    CombinedReservations4View,
    CombinedReservations6View,
    CombinedSharedNetworks4View,
    CombinedSharedNetworks6View,
    CombinedSubnets4View,
    CombinedSubnets6View,
    _fetch_all_leases_from_server,
    _fetch_leases_from_server,
    _fetch_reservations_from_server,
    _fetch_shared_networks_from_server,
    _fetch_subnets_from_server,
    _filter_subnets,
)

# DHCP enable/disable confirmation views
from .dhcp_control import (  # noqa: F401
    ServerDHCP4DisableView,
    ServerDHCP4EnableView,
    ServerDHCP6DisableView,
    ServerDHCP6EnableView,
)

# Lease list/delete/add/sync views + low-level helpers
from .leases import (  # noqa: F401
    BaseServerLeasesDeleteView,
    BaseServerLeasesView,
    ServerLease4AddView,
    ServerLease4EditView,
    ServerLease6AddView,
    ServerLease6EditView,
    ServerLeases4DeleteView,
    ServerLeases4View,
    ServerLeases6DeleteView,
    ServerLeases6View,
    _add_lease_journal,
    _enrich_leases_with_badges,
    _fetch_reservation_by_ip,
    _fetch_reservation_by_ip_for_leases,
)

# Options / option-def views + badge + IP panel
from .options import (  # noqa: F401
    CombinedServerStatusBadgeView,
    IPAddressKeaReservationsView,
    ServerDHCP4OptionsEditView,
    ServerDHCP6OptionsEditView,
    ServerOptionDef4AddView,
    ServerOptionDef4DeleteView,
    ServerOptionDef4View,
    ServerOptionDef6AddView,
    ServerOptionDef6DeleteView,
    ServerOptionDef6View,
    ServerSubnet4OptionsEditView,
    ServerSubnet6OptionsEditView,
)

# Reservation list/add/edit/delete views + helpers
from .reservations import (  # noqa: F401
    ServerReservation4AddView,
    ServerReservation4DeleteView,
    ServerReservation4EditView,
    ServerReservation6AddView,
    ServerReservation6DeleteView,
    ServerReservation6EditView,
    ServerReservations4View,
    ServerReservations6View,
    _add_reservation_journal,
    _build_reservation_options_formset,
    _enrich_reservations_with_badges,
    _enrich_reservations_with_lease_status,
    _filter_reservations,
    _get_reservation_identifier,
)

# Server CRUD views
from .server import (  # noqa: F401
    ServerBulkDeleteView,
    ServerBulkEditView,
    ServerBulkImportView,
    ServerDeleteView,
    ServerEditView,
    ServerListView,
    ServerStatusView,
    ServerView,
)

# Shared network views
from .shared_networks import (  # noqa: F401
    ServerSharedNetwork4AddView,
    ServerSharedNetwork4DeleteView,
    ServerSharedNetwork4EditView,
    ServerSharedNetwork6AddView,
    ServerSharedNetwork6DeleteView,
    ServerSharedNetwork6EditView,
    ServerSharedNetworks4View,
    ServerSharedNetworks6View,
)

# Subnet list / pool / subnet CRUD views + overlap warning helpers
from .subnets import (  # noqa: F401
    ServerDHCP4SubnetsView,
    ServerDHCP6SubnetsView,
    ServerSubnet4AddView,
    ServerSubnet4DeleteView,
    ServerSubnet4EditView,
    ServerSubnet4PoolAddView,
    ServerSubnet4PoolDeleteView,
    ServerSubnet4WipeView,
    ServerSubnet6AddView,
    ServerSubnet6DeleteView,
    ServerSubnet6EditView,
    ServerSubnet6PoolAddView,
    ServerSubnet6PoolDeleteView,
    ServerSubnet6WipeView,
    _warn_pool_reservation_overlap,
    _warn_reservation_pool_overlap,
)

# Sync job management views
from .sync_jobs import (  # noqa: F401
    ServerSyncNowView,
    ServerSyncStatusView,
    ServerSyncToggleView,
    SyncJobsView,
)

# Sync views + bulk import views
from .sync_views import (  # noqa: F401
    ServerLease4BulkImportView,
    ServerLease4SyncView,
    ServerLease6BulkImportView,
    ServerLease6SyncView,
    ServerReservation4BulkImportView,
    ServerReservation4BulkSyncView,
    ServerReservation4SyncView,
    ServerReservation6BulkImportView,
    ServerReservation6BulkSyncView,
    ServerReservation6SyncView,
    _BaseSyncView,
)
