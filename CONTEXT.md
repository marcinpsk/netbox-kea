<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba
SPDX-License-Identifier: Apache-2.0
-->

# Kea Network Management

This context manages Kea DHCP configuration and mirrors selected live Kea state into NetBox IPAM.

## Language

**Server**:
A configured Kea server that provides DHCPv4, DHCPv6, or both.
_Avoid_: Kea instance, endpoint

**Subnet**:
A Kea-managed IP network for one address family. Within a Server and address family, its CIDR and Kea subnet ID identify the same Subnet.
_Avoid_: Network

**Subnet Identity**:
The canonical CIDR and Kea subnet ID that identify one Subnet within a Server and address family. Both values must identify the same Subnet.
_Avoid_: Subnet key

**Shared Network**:
A named Kea grouping whose Subnets share configuration.
_Avoid_: Network, subnet group

**DHCP Link**:
A vendor-neutral, family-specific DHCP selection domain for Subnets on one client attachment or relay-selected link. Kea Shared Network, ISC DHCP shared-network, Microsoft Superscope, Cisco Network or Link, and Infoblox Shared Network are vendor implementations. A DHCP Link does not require an aggregate Prefix.
_Avoid_: Aggregate Prefix, Shared Prefix

**Aggregate Prefix**:
An optional IPAM aggregate that contains more-specific Prefixes. It does not define DHCP selection, allocation, or configuration inheritance.
_Avoid_: DHCP Link, Shared Prefix

**Pool**:
An inclusive address range within a Subnet from which Kea can allocate leases. Kea can express it as explicit endpoints or as a prefix.
_Avoid_: Range

**DHCP Option**:
A DHCP parameter assignment with an option space, a name or code, encoded data, and Kea delivery flags. The same value semantics apply wherever Kea assigns the option.
_Avoid_: Subnet option, reservation option

**Reservation**:
A Kea host-specific DHCP configuration for exactly one Reservation Identity. It can reserve no address, one IPv4 address, or multiple IPv6 addresses and delegated prefixes.
_Avoid_: Host record, static lease

**Reservation Identity**:
Within a Server and address family, the Reservation Scope plus exactly one Kea identifier type and value that identify a Reservation.
_Avoid_: Identifier priority, reservation key

**Reservation Scope**:
The place where Kea applies a Reservation. It is either Global or one specific Subnet.
_Avoid_: Subnet ID, reservation location

**Reservation Snapshot**:
A time-bounded observation of Reservations for one Server, address family, and requested scope. An Incomplete Reservation Snapshot preserves valid Reservations and identifies records that could not be interpreted.
_Avoid_: Reservation response, host list

**Reservation Synchronization State**:
The relationship between all addresses in one Reservation and their corresponding NetBox IP addresses. It is Not Applicable, Not Synchronized, Partially Synchronized, Synchronized, or Unknown.
_Avoid_: Lease status, sync badge

**Reservation Transfer Document**:
A YAML or JSON document that represents one or more Reservations with explicit Reservation Scope. The same structure supports export and proposed creation. The complete document must be valid before any creation starts.
_Avoid_: CSV import, CSV export, Kea payload

**Subnet Settings**:
The typed DHCP behavior that Kea currently applies to a Subnet after inheritance, such as lease timers, allocator selection, relay data, class restrictions, and DDNS settings. It does not state where a value was declared.
_Avoid_: Raw subnet configuration, settings dictionary

**Verified Subnet**:
A Subnet whose canonical CIDR and Kea subnet ID have been confirmed as one unique Subnet by the identity authority.
_Avoid_: Valid subnet

**Configured Subnet**:
A Subnet description derived from validated configuration facts without verified Subnet Identity. It cannot authorize identity-sensitive work.
_Avoid_: Unverified identity, fallback subnet

**New Subnet Identity**:
A proposed canonical CIDR and Kea subnet ID that a complete live identity observation confirms are available for creation.
_Avoid_: Free subnet ID, next subnet ID

**Subnet Catalogue**:
The complete canonical description of configured Subnets for one Server and address family. It includes identity, Shared Network membership, Pools, DHCP Options, and Subnet Settings.
_Avoid_: Subnet list, subnet choices

**Catalogue Snapshot**:
A time-bounded observation of one Subnet Catalogue. A Complete Catalogue Snapshot has valid and consistent required facts. An Incomplete Catalogue Snapshot preserves safe facts but identifies missing, invalid, or inconsistent facts. One missing or invalid Pool, DHCP Option, or Subnet Settings value makes the Snapshot incomplete, and the configuration facts that did parse are not authoritative for the Subnet.
_Avoid_: Response, raw configuration

**Identity-Only Catalogue Snapshot**:
An Incomplete Catalogue Snapshot that has verified Subnet Identity facts but lacks full configuration facts.
_Avoid_: Partial subnet list

**Configuration-Only Catalogue Snapshot**:
An Incomplete Catalogue Snapshot that has validated configuration facts but lacks verified Subnet Identity facts.
_Avoid_: Identity fallback
