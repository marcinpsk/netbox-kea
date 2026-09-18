---
status: accepted
date: 2026-08-13
---

# Concentrate Reservation record semantics

## Context

The repository interprets raw Kea host reservation dictionaries in views, forms, REST actions, synchronization,
jobs, transfer code, and the optional DHCP plugin adapter. These paths use different identifier lists, address
rules, option parsers, and matching rules. A valid reservation can reserve no address, one IPv4 address, or
multiple IPv6 addresses and delegated prefixes. It can be global or belong to one Subnet. Kea can also return
malformed records or fields that this plugin does not manage.

This duplication causes observable defects. Some paths accept `remote-id` even though it is relay data rather than
a native reservation identifier. Some paths choose the first of several identifiers instead of rejecting ambiguous
identity. Address-keyed actions cannot address reservations without an allocation. Multi-address IPv6 reservations
can receive a synchronization result based on only one address. Raw payloads also let concurrent edits overwrite
Kea fields that the plugin does not understand.

## Decision

Create one deep reservation module. It owns reservation identity, scope, normalization, validation, query semantics,
mutation semantics, diagnostics, transfer documents, and synchronization matching. Production callers do not
interpret raw Kea reservation dictionaries outside its private adapters.

The public model contains immutable IPv4 and IPv6 Reservation variants. Each Reservation has one address family,
one Reservation Scope, exactly one Reservation Identity, ordered allocation addresses, ordered delegated prefixes,
a hostname, and DHCP Options. Global and In-Subnet scopes are distinct. Zero or multiple identifiers make a record
malformed. Hardware address, DUID, and client identifier values use canonical lowercase colon notation. Circuit ID
and Flex ID remain exact opaque values. `remote-id` is not a Reservation Identity. User help explains how Kea Flex ID
can derive identity from relay remote ID.

The Kea client owns command selection, response-envelope validation, exact lookup, bounded pages, and page cursors.
Its reservation operations accept and return typed values. A Reservation Snapshot preserves valid records when
other records are malformed, reports safe diagnostics, and states whether the observation is complete. Exact lookup
or mutation of a malformed record fails closed.

Identity and scope are immutable during update. A change to either requires explicit delete and create operations.
An update refetches the latest raw record, checks a signed fingerprint of managed facts, applies explicit unchanged,
set, and clear operations, and preserves unknown Kea fields. A managed-fact conflict rejects the update. A concurrent
change to an unknown field does not cause a false conflict. Mutation results distinguish intended state, applied
state, persisted state, and verified state. Existing reservation signals use one typed before-and-after payload.

The module provides one shared immutable DHCP Option value and parser for Reservations, the Subnet Catalogue, and
the DHCP plugin adapter. Unknown reservation fields stay private and are preserved during read-modify-write. They do
not become an untyped public extras map.

UI and REST reads use bounded queries. REST accepts exactly one selector: bounded page, exact identity, scoped
address discovery, or hostname. It returns normalized records, diagnostics, completeness, and an opaque next cursor.
The REST surface remains read-only. Global Reservations are visible with their scope but have no mutation actions.
Creation and mutation support only In-Subnet Reservations. If identifier capabilities cannot be read from Kea, the
plugin keeps existing Reservations visible but disables mutation.

YAML and JSON Reservation Transfer Documents replace reservation CSV import and export. Both formats use the same
normalized record structure. The schema represents Global and In-Subnet scope, but creation rejects Global with a
specific unsupported-scope diagnostic. Import validates the complete document and reports every structural error or
duplicate before it sends a mutation. It then creates records in order, stops at the first Kea failure, and reports
created, failed, and not-attempted records. It does not attempt compensating deletes.

Synchronization processes every allocation address in each valid In-Subnet Reservation. It does not synchronize
Global Reservations or delegated prefixes into NetBox IPAM. It suppresses stale cleanup unless every page and record
for the requested scope completed successfully. Reservation-to-lease matching is scope-aware. In-Subnet matching
requires the same Subnet and either an allocation address or normalized identity. Global matching uses identity only.

The optional DHCP plugin adapter consumes the typed Reservation interface directly. It imports supported In-Subnet
and Global Reservations and reports quarantined records. It does not retain a separate reservation intent model. The
optional plugin holds a delegated prefix only as a NetBox Prefix, so the adapter creates that row to keep the fact.
This does not change the synchronization rule above, which still writes no IPAM row for a delegated prefix. The
adapter reports every reserved address it cannot attach, because a Global Reservation has no Subnet to size one from.

The replacement is completed in one change. Remove the old reservation intent, raw parsing helpers, duplicate
identifier constants, reservation CSV paths, and address-keyed mutation routes when their callers move. Do not keep
a compatibility path.

## Consequences

Callers share one definition of Reservation facts and cannot silently choose a different identifier or address rule.
Malformed live data remains visible as a safe diagnostic without discarding unrelated valid Reservations. Updates
preserve Kea extensions that the plugin does not understand.

The migration affects UI, REST, synchronization, jobs, transfer workflows, and the optional DHCP plugin integration.
Identity-keyed URLs and YAML or JSON transfer documents intentionally replace existing address-keyed URLs and CSV
reservation transfers.

Kea does not provide a transaction for a multi-record import. A stopped import can therefore contain successful
earlier creations. The result makes that partial state explicit.

## Rejected Alternatives

- Keep raw dictionaries at each caller: rejected because it caused identity, address, and malformed-data drift.
- Retain a second reservation intent for the DHCP plugin: rejected because it would recreate the duplicate model.
- Use address as mutation identity: rejected because a Reservation can have no address or multiple addresses.
- Treat relay remote ID as a native identifier: rejected because Kea requires Flex ID configuration for that use.
- Guess capabilities when Kea configuration is unavailable: rejected because mutation must fail safely.
- Add an untyped extras map: rejected because unknown values need preservation, not public interpretation.
- Roll back a partial import with deletes: rejected because concurrent change makes compensating deletes unsafe.
- Preserve old CSV and address-keyed paths for compatibility: rejected because the replacement must have one source
  of truth.
