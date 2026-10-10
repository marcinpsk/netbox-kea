# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-FileCopyrightText: 2026 Andrew Backeby <andrew@backeby.eu>
# SPDX-License-Identifier: Apache-2.0
import ipaddress
from typing import Literal, get_args

# The DHCP address families, and the value types their records carry. Defined here
# because both the Reservation domain and the Subnet Catalogue describe the same two
# families; a second definition can drift from this one without any error.
#
# The "Value" suffix is deliberate. netaddr exports its own IPAddress and IPNetwork,
# which this package also uses, and the two are not interchangeable: the same address
# built by each library compares unequal, so a mix-up makes a duplicate check miss.
# NetBox stores IPRange.size in a PostgreSQL integer column.
IP_RANGE_MAX_SIZE = 2_147_483_647

Family = Literal[4, 6]
IPAddressValue = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetworkValue = ipaddress.IPv4Network | ipaddress.IPv6Network

# Whether Kea wrote the running configuration to disk after a Configuration Change.
Persistence = Literal["persisted", "failed", "not-requested"]

BY_IP = "ip"
BY_HOSTNAME = "hostname"
BY_DUID = "duid"
BY_SUBNET = "subnet"
BY_SUBNET_ID = "subnet_id"
BY_HW_ADDRESS = "hw"
BY_CLIENT_ID = "client_id"

# The lease selector that finds every lease for one Reservation Identity without a
# Subnet. Kea has no lease query for the other identifier types, so they are absent
# and a Global Reservation that uses one reports no lease relationship.
LEASE_SELECTOR_BY_IDENTIFIER: dict[int, dict[str, str]] = {
    4: {"hw-address": BY_HW_ADDRESS, "client-id": BY_CLIENT_ID},
    6: {"duid": BY_DUID},
}

HEX_STRING_REGEX = r"^([0-9A-Fa-f]{2}[:-]?)*([0-9A-Fa-f]{2})$"

# id of the <datalist> that suggests subnet CIDRs on the reservation add form.
# Shared by the form widget's `list` attribute and the template that emits the element.
RESERVATION_SUBNET_DATALIST_ID = "kea-reservation-subnet-cidrs"

# Cache namespaces shared by the producers and test cleanup.
SERVER_CONFIGURATION_CACHE_PREFIX = "netbox_kea:server_configuration:"
SUBNET_CATALOGUE_CACHE_PREFIX = "netbox_kea:subnet_catalogue:"

# Maximum age of an interactive configuration or catalogue snapshot, in seconds.
DISPLAY_SNAPSHOT_TTL = 300

# kea/src/lib/dhcp
# RFC8415 section 11.1
DUID_MAX_OCTETS = 128
DUID_MIN_OCTETS = 1
CLIENT_ID_MAX_OCTETS = DUID_MAX_OCTETS
CLIENT_ID_MIN_OCTETS = 2

UINT32_MAX = 0xFFFFFFFF
# Kea requires Subnet IDs greater than zero and less than UINT32_MAX.
MIN_SUBNET_ID = 1
MAX_SUBNET_ID = UINT32_MAX - 1
# Kea stores an infinite valid lifetime as the largest uint32 (Lease::INFINITY_LFT).
INFINITE_LIFETIME = UINT32_MAX

# Kea lease states; the position of each is its Lease::STATE_* code (Kea 3.2.0 lease.h).
LeaseState = Literal["assigned", "declined", "expired-reclaimed", "released", "registered"]
LEASE_STATES: tuple[LeaseState, ...] = get_args(LeaseState)
LEASE_STATE_CODES: dict[LeaseState, int] = {state: code for code, state in enumerate(LEASE_STATES)}
# The states a Subnet lease query can filter by: stat_cmds counts only these.
LEASE_QUERY_STATES: tuple[LeaseState, ...] = ("assigned", "declined")

# Kea 3.2.0 lease identifier octets: HWAddr::MAX_HWADDR_LEN (hwaddr.h) and the DUID and ClientId sizes (duid.h).
LEASE_HW_ADDRESS_OCTETS = (1, 20)
LEASE_DUID_OCTETS = (3, 130)
LEASE_CLIENT_ID_OCTETS = (2, 255)

# Human-readable labels of the lease states that the lease UI shows.
# https://kea.readthedocs.io/en/latest/arm/lease-db.html#lease-states
LEASE_STATE_LABELS: dict[LeaseState, str] = {
    "assigned": "Active",
    "declined": "Declined",
    "expired-reclaimed": "Expired",
    "released": "Released",
    "registered": "Registered",
}

# The states that the lease search form filters by.
LEASE_FILTER_STATES: tuple[LeaseState, ...] = ("assigned", "declined", "expired-reclaimed")
LEASE_STATE_CHOICES = [("", "Any")] + [
    (str(LEASE_STATE_CODES[state]), LEASE_STATE_LABELS[state]) for state in LEASE_FILTER_STATES
]

StaleCleanupMode = Literal["remove", "deprecate", "none"]
STALE_CLEANUP_MODES: tuple[StaleCleanupMode, ...] = get_args(StaleCleanupMode)
