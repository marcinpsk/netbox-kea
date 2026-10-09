<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
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
A named Kea grouping whose Subnets share configuration. It carries its own settings and DHCP Option values, and
it can exist with no member Subnets.
_Avoid_: Network, subnet group

**DHCP Link**:
A vendor-neutral, family-specific DHCP selection domain for Subnets on one client attachment or relay-selected link. Kea Shared Network, ISC DHCP shared-network, Microsoft Superscope, Cisco Network or Link, and Infoblox Shared Network are vendor implementations. A DHCP Link does not require an aggregate Prefix.
_Avoid_: Aggregate Prefix, Shared Prefix

**DHCP Import Mapping**:
An association between a Kea source object and its imported NetBox DHCP object. The source identity includes
the Server, address family, and either a Kea subnet ID or a Reservation Identity in Global scope.
_Avoid_: DHCP Link, ownership link

**Aggregate Prefix**:
An optional IPAM aggregate that contains more-specific Prefixes. It does not define DHCP selection, allocation, or configuration inheritance.
_Avoid_: DHCP Link, Shared Prefix

**Pool**:
An inclusive address range within a Subnet from which Kea can allocate leases. Kea can express it as explicit endpoints or as a prefix.
_Avoid_: Range

**DHCP Option**:
A DHCP parameter assignment identified by an option space and a code or name. It includes encoded data and Kea delivery flags. Its declaration scope determines which clients receive it. The same value semantics apply wherever Kea assigns the option.
_Avoid_: Option, option-data, Subnet option, reservation option

**Lease**:
A Kea record of a DHCP address allocation or an IPv6 delegated-prefix allocation. It describes the allocation's
state and client information within one Server and address family.
_Avoid_: Reservation, static lease

**Lease Identity**:
Within one Server and address family, the allocation kind and canonical allocation address identify one Lease
in Kea. Client binding, Subnet and delegated prefix length describe the observed allocation.
_Avoid_: Lease IP, lease key

**Current Lease**:
A Lease in the assigned state, or an address Lease in the registered state, whose valid lifetime has not ended.
An infinite valid lifetime does not end.
_Avoid_: Active IP address, database lease

**Lease Snapshot**:
A time-bounded observation of Leases for one Server, address family and requested scope. An Incomplete Lease
Snapshot preserves valid Leases with diagnostics and cannot establish the absence of a Lease.
_Avoid_: Lease response, lease collection

**Reservation**:
A Kea host-specific DHCP configuration for exactly one Reservation Identity. It can reserve no address, one IPv4 address, or multiple IPv6 addresses and delegated prefixes.
_Avoid_: Host record, static lease

**Reservation Identity**:
Within a Server and address family, the Reservation Scope together with exactly one Kea identifier type and value. All three parts are necessary. The identifier type and value alone do not identify a Reservation, because the same identifier can name one Global and one In-Subnet Reservation.
_Avoid_: Identifier priority, reservation key

**Reservation Scope**:
The place where Kea applies a Reservation. It is either Global or one specific Subnet.
_Avoid_: Subnet ID, reservation location

**Published Name**:
The hostname that Kea gives a client for a Reservation: the stored Reservation hostname under the Effective DDNS
Qualifying Suffix, in lower case and without a trailing dot. The Reservation forms take it, and the IPAM
synchronization writes it to NetBox for the Reservation source. The lease source writes the observed Lease hostname,
which Kea stores already qualified, without a trailing dot.
_Avoid_: Stored hostname, qualified name, FQDN

**Effective DDNS Qualifying Suffix**:
The `ddns-qualifying-suffix` that Kea applies to one address: the value of the Pool that contains the address,
else of its Subnet, its Shared Network, or the global configuration, in that order. It is unknown when a Pool of
the Subnet sets a value and the address is not known.
_Avoid_: Subnet suffix, domain suffix

**Reservation Snapshot**:
A time-bounded observation of Reservations for one Server, address family, and requested scope. An Incomplete Reservation Snapshot preserves valid Reservations; failed or bounded page reads can make it incomplete without a record diagnostic, while parsing failures identify records that could not be interpreted.
_Avoid_: Reservation response, host list

**Reservation Synchronization State**:
The relationship between all addresses in one Reservation and their corresponding NetBox IP addresses in the sync VRF of the Server. It is Not Applicable, Not Synchronized, Partially Synchronized, Synchronized, or Unknown.
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
The complete canonical description of configured Subnets for one Server and address family. It includes identity, Shared Network membership, Pools, DHCP Option values, and Subnet Settings.
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

**Option Definition**:
A declaration that gives a custom DHCP Option its code, name, option space, and data type. It defines an option.
It does not assign a value.
_Avoid_: option-def, custom option

**Server Configuration**:
The complete declared DHCP configuration that one Server applies for one address family. It includes Subnets,
Shared Networks, server-global DHCP Option values, and Option Definitions.
_Avoid_: Raw config, config-get response

**Server Configuration Snapshot**:
A time-bounded observation of one Server Configuration. It states whether the observation is available and
whether every fact parsed. Valid facts survive an invalid one.
_Avoid_: Configuration Snapshot, Dhcp4 dict

**Configuration Change**:
One operator-requested change to a Server Configuration, such as adding a Subnet or moving it to another
Shared Network. It can need several Kea commands, and it is applied, rejected, or unconfirmed as a whole.
_Avoid_: Config write, config mutation, config-set

**Configuration Change Outcome**:
The observed result of a Configuration Change that is live or can be live. It states whether the change is
applied or unknown, and whether it is persisted, failed to persist, or had no persistence requested.
_Avoid_: Partial persist, ambiguous config-set

**IPAM Ownership**:
The fact that one Server and address family synchronized a NetBox IP address, Prefix, or IP Range from one Kea
source: an address lease, a delegated-prefix lease, a Reservation, a Subnet, a Pool, or a Reservation delegated
prefix. Only a Current Lease is a lease source. One object can have several owners. An operator
ends every ownership of an object when the description no longer starts with a well-formed marker block
(`[kea-sync: <kind>]`): the block is missing, moved or malformed. A block of another known kind still marks the
object. Text after the block is an operator note, and the synchronization keeps it.
_Avoid_: Kea-managed IP, synced description

**Stale IPAM Object**:
An owned NetBox object that a complete synchronization of its owner no longer reports. The owner drops its
ownership. The object is removed or deprecated only when no owner is left.
_Avoid_: Ghost IP, stale IP, orphan
