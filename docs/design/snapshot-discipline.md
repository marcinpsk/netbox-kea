---
status: accepted
date: 2026-10-02
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Preserve persisted values before a NetBox update

NetBox builds `ObjectChange.prechange_data` from the loaded object's snapshot.
Each current update calls `snapshot()` before its first field change, including
changes made by a mutation helper. A later snapshot can overwrite the old values.
A successful save ends the update cycle. New objects need no prechange snapshot
before their first save. Plain bookkeeping models do not use NetBox change records.

The update functions own direct snapshot calls. There is no runtime wrapper or
model inheritance change. Real request and entrypoint tests assert old and new
ObjectChange values for IPAM, MAC addresses, DHCP imports and the sync toggle.

## Direct-save inventory

`netbox_kea/tests/snapshot_discipline.py` scans production Python modules. Tests
and migrations are excluded. The native suite and pre-commit hook run the same
scanner. The inventory names the module, function, direct receiver, classification
and save count for each current site. It covers 25 saves: 15 loaded updates and
10 explicit create, plain bookkeeping or framework sites. An unclassified direct
save or a changed save count requires an inventory review.

For loaded sites, a small local walk checks standalone snapshots, direct field
assignments, `setattr()` and two named mutation helpers: `_update_mac_description`
and `_apply_reservation_identifier`. The receiver must be the direct first argument
of a recognized helper. The current Option, ClientClass and HostReservation
branches also admit their explicitly named constructors before a first save.

The walk follows both sides of an `if`, the body of a `with`, and the existing
try/handler layout. Loops use zero and one iteration paths. A loaded save needs
an earlier snapshot, and a snapshot after a detected mutation fails. Saving
consumes protection. The inventory is review metadata, not a violation baseline:
new save sites must be classified rather than silently accepted.

## Limits

This guard checks the current direct-receiver update patterns. It does not prove
model types, imports, class construction, decorators or acquisition-helper effects.
It does not follow heap aliases, iterator-to-index identity, conditional aliases,
nested mutable field aliases, dynamic persistence references or opaque helpers.
It does not model arbitrary Python expression order or all exception and repeated
loop paths. Those forms can pass this narrow check and still require code review.
Changing an inventoried exception into a loaded update also requires review.

Use direct snapshot and mutation statements for the current update paths. Review
new helper behavior and persistence classifications with real change-record tests.
If a new pattern needs mechanical coverage, add a small reliable check for that
pattern instead of widening this guard into a general Python interpreter.

## Design and verification

Independent design and adversarial review established the snapshot invariant.
Repeated attempts to prove arbitrary receiver provenance produced a large guard
with further bypasses. The final scope uses a small explicit persistence inventory
and checks the current direct patterns. It accepts the alias and dynamic limits
above. The runtime snapshot fix and its real ObjectChange tests remain intact.

Each of the fifteen runtime snapshots is removed and moved after mutation in
memory. All thirty variants must produce a snapshot or mutation diagnostic.
Nearby controls cover early, missing, late, conditional and wrong-receiver
snapshots, the mutation helpers, new saves and changed save counts. The
production tree and hook provide passing controls. Native focused tests, Ruff,
format checks and the zero-new-error mypy gate precede final whole-suite validation
and a scoped adversarial review.

The mutation controls cover each inventoried direct loaded-receiver save. The DHCP mapping model adapter
loads a persisted row and delegates the original save callable. This is outside direct-save AST inference.
Real native update_or_create history tests prove that adapter snapshot preserves the old association.
