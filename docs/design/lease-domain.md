---
status: ratified
date: 2026-10-03
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Typed Lease observations

This design session replaces raw Lease dictionaries at the domain interface. The accepted decisions below
record the operator's answers. Issue 291 implements the typed values and observation contracts in
`netbox_kea/leases.py`, with tests. Issue 292 moves every Lease read to them: KeaClient reads, browsing,
combined views, exports, REST, Reservation matching and the IPAM lease phase. Issue 293 moves the
actions to them: Edit and Delete carry the `ShownLease` facts and read the target again in `KeaClient`
before a change, creation and CSV import send typed requests, Reserve fills the prefix field for a
delegated prefix, and the lease signals carry typed values. Issue 294 adds the `lease-prefix` ownership
source: the job reads one Lease Snapshot for its address and delegated-prefix phases, both take only Current
Leases, a manual Sync claims a delegated prefix as a Prefix, and a recorded allocation kind repairs the IP
addresses that earlier releases made of delegated prefixes.

## Accepted decisions

- Use strict, frozen Pydantic v2 models as the typed Lease values. Declare Pydantic as a direct runtime
  dependency. The lock file currently includes it through development release tooling only.
- Validate records individually. Preserve valid records and report diagnostics when a record is malformed.
  Such an observation is incomplete. It cannot authorize stale cleanup or establish that a Reservation has
  no Lease. Invalid response envelopes or unusable pagination cursors fail the read.
- Represent address allocations and IPv6 delegated-prefix allocations explicitly. Preserve the allocation
  kind and the delegated prefix length.
- Support the existing Edit, Delete, Reserve and Sync actions for both allocation kinds. Carry allocation
  kind through selection, lookup and mutation. A new manual delegated-prefix creation form is outside this
  change.
- Synchronize delegated-prefix leases to NetBox Prefixes in the Server's configured VRF. Use the existing
  shared ownership policy. Never delete Prefixes. Deprecate them only when the Server enables it. Keep live
  lease evidence separate from Reservation-import evidence.
- Preserve unknown Kea fields privately and carry them through edits. Typed callers consume the fields
  the model understands. An edit of known fields must preserve extension values without exposing mutable
  dictionaries through the frozen model.
- Before editing or deleting, read the allocation again and compare client binding, Subnet, allocation
  kind, delegated prefix length and edited fields with the shown values. Refuse a conflict. Changes to
  ordinary renewal timestamps alone do not cause a conflict. This is a preflight check, not an atomic
  compare-and-write operation on Kea.
- Only Current Leases establish live IPAM ownership and positive Lease relationships. These are
  unexpired assigned or registered address allocations, and unexpired assigned delegated prefixes.
  Infinite lifetimes remain current. Inactive records remain visible. Unknown state or unusable lifetime
  produces diagnostics and blocks absence-based cleanup.
- Refuse a complete export when its observation is incomplete, including excluded malformed records.
  Show the reason and retain valid rows in the page. An explicitly labeled current-page export can
  download the displayed subset.
- Require observed fields that establish allocation identity, state and lifetime. Accept fields that
  Kea permits to be absent, and empty identifiers where its allocation state permits them. Do not
  invent observation values. Creation requests have a separate schema because Kea supplies some
  observed values after creation.
- Automatic delegated-prefix lease synchronization requires both effective Lease sync and effective
  Prefix sync. Apply the global and per-Server settings. Update their descriptions instead of adding
  a separate toggle.
- Public plugin contracts may change when the change improves Lease semantics. Compatibility with
  the current Lease REST response is not a constraint. Publish a documented projection of the typed
  domain with allocation kind and observation completeness. Keep presentation labels and private
  Kea extension values outside that projection. Use the existing typed Reservation API as the local
  precedent for normalized results and diagnostics.
- A blank hostname on edit clears the hostname. A blank client identifier or lifetime preserves
  the existing value. State this behavior in the form help text and represent it as explicit edit
  intent before changing Kea.
- Manual single-Lease Sync remains available when automatic synchronization is disabled. Require
  unconstrained IPAddress and MACAddress add and change permissions for address allocations, and
  unconstrained Prefix add and change permissions for delegated prefixes. Preserve the existing explicit adoption of unmanaged objects. Require a
  fresh Current Lease and verified allocation facts. A single-Lease claim never cleans up other
  objects.
- Repair IPAddress ownership previously derived from delegated-prefix leases through fresh
  reconciliation, not a bulk conversion. Confirm the correct Prefix before retiring the old Lease
  ownership link. Preserve operator-managed objects and other owners. Apply configured stale-IP
  cleanup only after complete observations satisfy the existing cleanup safeguards.
- Replace the internal Lease dictionary path across browsing, search, exports, REST, edits,
  deletion, Reservation matching and IPAM synchronization in one complete change. Remove obsolete
  Lease parsing paths. Broader Kea transport changes and unrelated domains are outside this scope.

Pydantic validates field types and constraints. Domain operations still own observation completeness,
Subnet fact authority and ownership policy. Frozen models do not make nested dictionaries immutable.

## Evidence

`KeaClient.lease_search` and its page reader currently validate address shape and family. The UI adds
presentation fields in `utilities.format_leases`. Reservation matching validates state and identifiers.
IPAM Reconciliation validates Subnet ID and hostname, isolates invalid rows and protects stale cleanup.
These consumers currently receive dictionaries and apply different validation policies.

`KeaClient.lease_update` modifies a fresh raw record and sends it back. This preserves unknown fields.
The replacement keeps that preservation contract without exposing mutable wire dictionaries.

The lease IPAM phase currently treats IPv6 records as addresses. The existing delegated-prefix phase
reads Reservations supplied by the DHCP import. Sharing one source between those independent observations
could let one observation remove the other's ownership links.

Lease edits fetch fresh raw fields but do not compare them with the values shown on GET. Lease IPAM
reports do not inspect state or lifetime. Export All refuses truncation but has no per-record diagnostic
policy because the current collection has no completeness field.

The Lease REST actions currently return UI-enriched dictionaries with `count` and `results` only.
The Reservation REST actions already return normalized typed records with diagnostics, completeness
and a continuation cursor. The Lease API can follow that local convention without exposing its UI
format or Kea's raw extension fields.

## Specification r2

The operator selected a parent specification ticket and implementation tickets as the next deliverable.
These tickets do not authorize runtime implementation. This specification makes the accepted
behavior concrete and carries the independent design and adversarial review recorded below.

### Module and interface

Add `netbox_kea/leases.py` as the owner of Lease values, record validation, observation completeness,
current-use evaluation, public record projection and edit comparison. It depends on the existing
family constants and standard value types. It does not import Django views or IPAM models.
`KeaClient` retains the existing transport and command dispatch. Its Lease methods use this module
and return typed values. IPAM writes remain in reconciliation. Presentation stays in the UI adapter.

Use strict, frozen Pydantic v2 values for the interpreted Lease schema, identity, diagnostics,
Snapshot and shown edit values. Use tuples and immutable nested values. Do not add matching
dataclasses, mutable extra dictionaries or a second transport adapter. Add the module to the exact
wire-owner list and remove superseded baseline sites. Do not expand the checker into a new analyzer.

The external interface consists of typed exact lookup, scoped search, page traversal, bounded full
collection, create, edit and delete. Creation and edit intent have their own types. An exact lookup
returns a valid Lease, confirmed absence, or a failed observation. Malformed exact results are never
confirmed absence. Callers do not repeat wire parsing or state/lifetime interpretation.

### Values and record validation

Represent DHCPv4 addresses, DHCPv6 addresses and DHCPv6 delegated prefixes as explicit typed variants.
The public kind is address or delegated-prefix, with family recorded separately. The internal union
must use an unambiguous variant discriminator. Allocation identity includes family, canonical address
and kind within the Snapshot's Server. Subnet, binding and PD length remain shown facts, not a way to
silently retarget an action.

Require canonicalizable address, supported kind, positive bounded Subnet ID, known state, valid
transaction time and nonnegative bounded valid lifetime. Require IPv6 DUID, IAID and preferred
lifetime; PD also requires a valid prefix length and canonical prefix base. Reject booleans as
integers and reject numeric strings. Check family and cross-field allocation constraints. Preserve
Kea-permitted empty identifiers in inactive records without inventing a matchable client identity.
Validate emitted hostname and FQDN fields with their actual types. Conditional hardware address,
client ID and pool ID remain optional where Kea permits omission. Unsupported kind/state, invalid
identifier, unusable time and overflow become stable field diagnostics.

For pinned Kea 3.2.0, PD prefix length is 1 through 128 inclusive and the address must be its
canonical base. Require an existing explicit Subnet association for PD facts, but do not require
the PD network to be contained in the Subnet's address CIDR. Kea permits a separate PD pool network.
IA_TA is not a supported wire type in this release; treat it as an unsupported-kind diagnostic.

Use the pinned Kea source and recorded real replies to define state and lifetime bounds. States are
assigned, declined, expired-reclaimed, released and registered. Registered applies to address
allocations. Finite expiration and infinite lifetime follow Kea's semantics. Use one explicit aware
evaluation time for an observation, and re-evaluate a fresh record before mutation or manual Sync.
Never label a Lease current just because its address is valid. Inactive records remain displayable.
The infinite sentinel is `0xffffffff`. A finite lifetime has expired when CLTT plus lifetime is
strictly less than the evaluation time in whole seconds (the evaluation time rounded down, as Kea
compares against `time(NULL)`). Registered allocations do not bypass expiration.

Keep wire documents private to KeaClient operations. During an edit, read a fresh raw body, validate
that same body into the immutable Lease, compare shown facts, then change only explicit fields in
that fresh body. Do not store extension dictionaries in Lease values or expose them to callers.
Preserve nested extension values, including values changed by an external writer after the form was
shown. Do not replay the GET document during POST. This preserves fields without serializing every
observed raw document into every Lease.

### Observation and traversal

A Snapshot identifies Server, family, requested query scope, aware read interval, records,
diagnostics and continuation. Distinguish a successfully validated page from exhaustive coverage
of the requested scope. Only exhaustive, diagnostic-free coverage can establish absence, complete
export or stale cleanup. A state or identity filter never attests to the whole daemon.

Validate the envelope before records. Validate records separately and retain good siblings.
Diagnostics include a stable code, field and source position, without rejected raw values or
exception details. If the allocation kind is unknown, the uncertainty applies to both potential
ownership sources. Do not count a dropped row as empty data.

Derive continuation and resource accounting from raw page evidence, not surviving records. If a
page's last raw address is valid, continue even when every record was quarantined. A malformed
cursor, non-progressing page or unusable envelope fails the read. Enforce existing collection caps
against raw records processed. Reaching a cap without proving the end is incomplete.

Stock Kea 3.2.0 backends require unique canonical IPv6 lease addresses, independently of kind.
Address continuation therefore cannot split two coexisting stock allocations at the same address.
Still carry kind for exact commands and protect against a fresh allocation changing kind.
Traversal is a time-bounded observation, not an atomic database snapshot. Retain existing cleanup
cutoffs and do not promise atomic consistency while Kea changes during reads.

### Mutations and consumers

Carry typed identity through routes, selection, badges, exact queries, edit, delete, reserve and Sync.
For PD reserve, write a delegated prefix into the Reservation, not an address. Do not add a manual
PD creation form. Existing creation and CSV import use typed request values and read the observed
Lease when an operation needs facts not present in the request.

Use the existing shown-value form pattern for edits and delete confirmation. Compare fresh binding,
Subnet, kind, PD length and written field values before sending a mutation. The binding includes
the protocol's identifier facts, including IPv6 IAID. Ignore ordinary renewal timestamps as conflicts,
but preserve their fresh values in the mutation document. Never force-create an allocation that
disappeared. The preflight does not exclude a race with another writer after the fresh read.
Derive written fields from normalized shown values and explicit intent. An unchanged prefilled
field is not an edit. A blank keep-current field also writes nothing, so a stale form cannot replay
an unchanged lifetime over a fresh renewal. Compare written fields, not untouched form fields.

Reservation matching and IPAM badges use only Current Leases as positive live evidence. Negative
matches require complete coverage of the relevant query. Keep display formatting separate from
typed Lease facts. Incomplete results remain visible with their reasons.

### Ownership and upgrade repair

Keep address Lease ownership under the existing lease source. Add a distinct IPv6 live-PD source,
`lease-prefix`, separate from the Reservation import's `delegated-prefix`. Build both
phases from one Lease observation with one pre-read confirmation cutoff. Successful valid claims
may proceed from a partial observation, but no affected source cleans up or attests to completion.

Update the ownership source enum, source/object-field inventory, receipt reader/writer, effective
source policy, job phase construction and completion coverage together. An old receipt does not
attest to the new source. A disabled source neither publishes success nor cleans up its links.
Preserve current shared locks, release markers, VRF handling, adoption barriers and Prefix
deprecation rules. A failed Prefix claim cannot be a successful replacement.

Confirm the replacement Prefix ownership before retiring a legacy address-Lease link for a PD
record. Keep that legacy link when Prefix sync is disabled, the target claim fails or the observation
is incomplete. Repeated runs must converge without guessing from an IPAddress alone. The repair
mechanism must remain safe if Kea no longer reports the old PD record; unresolved ambiguity retains
the object and reports it. Other owners and operator-managed objects remain protected. Prefixes
are never deleted. This is an upgrade path for existing OSS installations, not a second Lease parser.

Persist allocation-kind provenance on address Lease ownership links. Existing IPv6 address Lease
links start unclassified because their stored hostname and mask cannot prove the original kind.
Fresh known address observations classify address provenance. A fresh PD observation permits repair
only after its replacement claim succeeds. An unclassified historical link absent from Kea remains
protected and appears in the repair report until an operator releases it or later evidence classifies
it. New links record their kind immediately. Keep this metadata separate from the existing `_Facts`
payload, whose reader currently accepts exactly hostname and prefix length. This schema migration
does not guess ownership or convert IPAM objects.

### Public contracts

Follow the existing Reservation endpoint convention: `count`, normalized `results`, safe
`diagnostics`, `complete` and `next_cursor`. Include query scope and coverage so a valid page does
not imply a complete catalogue. Publish family, allocation kind, canonical address, PD length when
applicable, binding, Subnet, state, lifetime and aware expiration metadata. Infinite expiration has
an explicit representation. Do not expose extension documents or presentation labels.

Use documented CSV columns with family/kind and PD prefix length. Complete CSV refuses an incomplete
observation; a current-page CSV explicitly identifies its limited coverage. Creation import fields
are a separate contract, not a replay of an observed dump.

Lease signals carry typed allocation facts and confirmed outcomes, following the Reservation
signal convention. Do not report a delegated prefix only as an IP address. A creation signal cannot
invent an observed Lease when no readback exists. Failed or conflicted mutations emit no success
event. Update downstream-facing documentation for intentional contract changes.

### Replacement and acceptance

Remove LeaseFields, raw observational return contracts and duplicated state/lifetime interpretation
from KeaClient consumers, utilities, Reservation matching and reconciliation. UI dictionaries may
remain only as presentation projections. Raw Kea documents remain only inside wire owners. Update
the direct dependency, wire-owner gate, typed callers and existing tests in the same completed change.

Acceptance is observed through real request, form, KeaClient and ORM behavior:

- Mixed valid and malformed records preserve good rows and forbid cleanup, negative relationships
  and complete export. An empty surviving page still continues from a valid raw cursor.
- Booleans, numeric strings, missing mandatory fields, unknown states/kinds and invalid lifetimes
  produce diagnostics. Recorded real assigned, inactive, registered and infinite replies validate.
- Address and PD actions send the correct kind. PD Reserve creates a prefix Reservation; PD Sync
  creates or links a Prefix in the configured VRF and requires Prefix permissions.
- External binding, Subnet, kind, length or written-field changes cause no mutation. Renewal alone
  permits an edit that preserves fresh renewal and nested extension values. Missing targets are
  not recreated. Blank edit behavior matches the accepted form semantics.
- Manual Sync works with automatic flags off, but cannot claim an inactive/malformed allocation,
  bypass permissions, skip fact authority or clean up unrelated objects.
- Independent imported and live PD owners do not clean up each other. Failed, disabled or partial
  replacement leaves legacy IP ownership protected. Successful repair preserves shared owners and
  applies the configured address cleanup policy only after its complete-observation safeguards.
- New source enablement cannot reuse an old narrower receipt as completion. Source toggles, Run
  Now, import-only owners and concurrent claims retain the existing barrier behavior.
- REST and CSV have documented kind/coverage contracts, contain no private raw data and refuse
  false complete exports. Signals describe only confirmed outcomes.
- All first-party observation consumers use typed values; no obsolete parser or second writer
  remains. Existing branch-write refusal and configuration-change guards still apply.

For each corrected defect, first demonstrate the reachable red regression against the old path.
Stub only the external HTTP boundary and drive the real KeaClient. Use the real NetBox test database
through `kea-testdb`, recorded Kea replies and real daemon/browser integration for wire behavior.
Run the repository's configured parallel suite, type, lint, formatting, SPDX and wire-discipline
gates. Keep PRs below the operator's 100-file cap. No runtime gates have run during this design session.

### Ticket increments

The parent holds the complete specification and release acceptance. Child tickets describe coherent
implementation increments, not permission to ship a half-replaced path:

1. Typed domain, immutable wire preservation, observed/create/edit schemas and recorded fixtures.
2. Typed Kea reads and bounded traversal; migrate read consumers, matching and public projections.
3. Kind-aware mutation, shown checks, Reserve and typed signal outcomes.
4. Live-PD ownership, effective sources, receipts, manual claims and legacy ownership repair.
5. Final removal audit, real-daemon/browser coverage, public migration notes and release gates.

The final release requires all increments. No runtime implementation starts as part of ticket creation.

## Section 0: refuted claims

Claim: stock Kea 3.2.0 can store IA_NA and IA_PD at one canonical address, and address-only pagination
can skip one when a page splits the group. Refuted by memfile insertion uniqueness and both SQL
address-only primary keys. Reopen for a custom backend or a changed Kea storage contract. Allocation
kind still belongs in command identity and fresh mutation checks.

## Design review status

Revision r2 merges the coordinator's independent r1 with a fresh-context GPT-6.1 Sol high design.
The blind designer received only selected user constraints, source facts and acceptance predicates.
It did not read this record. Both designs were completed before comparison. The designer ran in a
read-only sandbox and changed no files or databases.

| Decision | Coordinator r1 | Blind design | Evidence and disposition | Consequence |
| --- | --- | --- | --- | --- |
| Module seam | Lease domain behind KeaClient | Same | Current callers already use KeaClient. Select this seam. | No speculative second transport. |
| Extension preservation | Immutable serialized wire data per Lease | Fresh private body during mutation only | Current lease_update already uses a fresh body. Select blind design. | Fewer stored values; nested extras never enter frozen models. |
| Registered lifetime | Require unexpired registered address | Consider expired registrations positive | User selected unexpired; Kea expired() also tests finite lifetime independently of state. Reject blind exception. | One current-use rule. |
| Unchanged edit fields | Compare written values | Derive edits before comparing | Forms are prefilled and current code writes unchanged fields. Select explicit delta plus shown comparison. | Renewals cannot replay untouched lifetime fields. |
| Envelope and cursor failure | Fail the read | Preserve earlier rows as incomplete traversal | User selected failed read for unusable envelope/cursor. Keep that contract. | No invented continuation or confirmed absence. |
| Ownership sources | lease and lease-prefix | Same | Import has its own delegated-prefix phase. Select separate live source. | Neither workflow attests to the other. |
| Legacy ambiguity | Persist provenance, retain unknown history | Retain unknown history, mechanism unspecified | Stored _Facts lacks original kind. Select persisted provenance. | Absent historical IPv6 ownership is reported, not guessed. |

Astra high reviewed revision r2 in an enforced read-only sandbox and returned RATIFY for the complete
design and five work-package ticket scope. It found no unresolved design blocker and no additional
user decision. The coordinator checked the relevant ownership removal and creation readback sites.
No runtime validation or implementation is claimed.

The review confirmed the module seam, separate provenance, independent source receipts, legacy
protection, fresh-body edit deltas, scope-aware completeness and test predicates. These design claims
are CLOSED for ticket publication. Runtime acceptance remains open until implementation and tests.
The proposed tests assert mutations, ownership rows, exports and event payloads, not only model flags.

Implementation notes from the review refine the existing acceptance predicates without changing r2:

- Protect unclassified legacy ownership in ordinary cleanup and opportunistic stale-link removal.
  Exercise another owner claiming an object whose unclassified historical link is already stale.
- Post-create synchronization always needs a fresh observed Current Lease, even when the request
  supplied a Subnet. A successful creation event does not imply a successful readback.
- Test unchanged prefilled lifetime after renewal, nested extension changes by another writer and
  failure to complete newly enabled source coverage from an older narrower receipt.

## Published tickets

The complete specification is [issue 290](https://github.com/marcinpsk/netbox-kea/issues/290).
Its native sub-issues are:

- [291: typed domain and observation contracts](https://github.com/marcinpsk/netbox-kea/issues/291).
- [292: typed reads and public results](https://github.com/marcinpsk/netbox-kea/issues/292).
- [293: kind-aware actions and stale edit refusal](https://github.com/marcinpsk/netbox-kea/issues/293).
- [294: live-PD ownership and upgrade repair](https://github.com/marcinpsk/netbox-kea/issues/294).
- [295: final replacement and public contract validation](https://github.com/marcinpsk/netbox-kea/issues/295).

Native blocking relationships follow domain, then reads, then actions and IPAM in parallel, then
final validation. Exact ticket contents, all five parent relationships and all five dependency edges
were verified through GitHub API readback. Issue 291 is closed: `netbox_kea/leases.py` and its tests
deliver it. Issues 290 and 292 to 295 remain open.

## Sources

- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/).
- [Pydantic model immutability](https://docs.pydantic.dev/latest/concepts/models/#faux-immutability).
- [Kea 3.2 lease schemas](https://kea.readthedocs.io/en/kea-3.2.0/api.html#lease6-get).
- [Kea 3.2 lease state and expiration implementation](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/lib/dhcpsrv/lease.cc).
- [Kea 3.2 delegated-prefix parser](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/hooks/dhcp/lease_cmds/lease_parser.cc).
- [Kea 3.2 PD pool containment invariant](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/lib/dhcpsrv/subnet.cc).
- [Kea 3.2 memfile Lease uniqueness](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/lib/dhcpsrv/memfile_lease_mgr.cc).
- [Kea 3.2 PostgreSQL Lease schema](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/share/database/scripts/pgsql/dhcpdb_create.pgsql).
- [Kea 3.2 MySQL Lease schema](https://github.com/isc-projects/kea/blob/Kea-3.2.0/src/share/database/scripts/mysql/dhcpdb_create.mysql).
