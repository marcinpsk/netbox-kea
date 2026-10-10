---
status: accepted
date: 2026-09-27
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# The IPAM synchronization owns NetBox objects through links

## Context

The IPAM synchronization treats a NetBox IP address as its own when the description starts with
"Synced from Kea DHCP", or is blank. Stale cleanup matches IP addresses by `dns_name` in all of NetBox. It has
no Server or VRF scope:

- The job of Server A deletes an IP address of Server B that has the same hostname. The next run of Server B
  creates it again, so the row changes on every interval.
- The per-row lease Sync keeps only one address, so it deletes a reserved address of the same host.
- Cleanup runs only for hostnames in the current run. The row of an expired lease stays forever. A lease
  without a hostname is never cleaned up.
- `get_netbox_ip` returns the first IP address in any VRF. Lease and Reservation rows go to the global VRF, and
  `Server.sync_vrf` applies only to Prefixes and IP Ranges. Two Servers with overlapping address space in two
  VRFs write to one row.
- Prefixes and IP Ranges have no cleanup.

Four callers run the synchronization: the job, the bulk Reservation view, the per-row Sync and lease add, and the
DHCP plugin import. Each one applies its own cleanup policy.

## Decision

### Ownership

A new link model records IPAM Ownership: `(server, family, source, object)`. The object is exactly one of three
nullable foreign keys, `ip_address`, `prefix` or `ip_range`, each with `on_delete=CASCADE`. A check constraint
requires exactly one. The source is `lease`, `lease-prefix`, `reservation`, `subnet`, `pool` or `delegated-prefix`.
`lease` owns the IP address of a Current address Lease, and `lease-prefix` owns the Prefix of a Current DHCPv6
delegated-prefix Lease. The DHCP plugin import owns the delegated prefixes of Reservations through
`delegated-prefix`, so the live and the imported delegated prefix never clean up each other. The link is
unique per `(server, family, source, object)`. Each link also stores the facts that its owner last reported for
the object.

An object is owned when it has at least one link and its description still starts with the marker. The marker is a
block at the start of the description: `[kea-sync: <kind>]`. The kind is `lease`, `reservation`,
`lease + reservation`, `subnet`, `delegated prefix` or `pool`. An operator can write a note after the block, usually
after one space. A description that is only the block has no trailing space. `netbox_kea/ipam_marker.py` owns this
format, and every reader and writer of the marker uses it.

The sync reads and rewrites only the block. It never adds or removes a space: it keeps whatever follows the block,
byte for byte, also text directly after it, such as `, rack 4` in `[kea-sync: lease], rack 4`. An operator releases
the object when the description does not start with a well-formed block of a known kind (or with the legacy marker
below): the block is missing, moved or malformed, for example `[kea-sync: lease ]` or `rack 4 [kea-sync: lease]`. A
note after the block does not release the object. A block of another known kind is still a marker: an edit from
`[kea-sync: lease]` to `[kea-sync: pool]` does not release the object, and the next write rewrites the kind. The
next run drops the links and reports a conflict. It does not remove or deprecate the released object, even when it
drops the last link.

The NetBox description holds at most 200 characters. When the new block and the kept text do not fit, the run does
not change the object and does not cut the text. It keeps and confirms the link of its owner, as after an owner
disagreement, and reports a conflict. When a cleanup removes a link that is not the last one and the new status
does not fit, the stale link stays and the run reports a conflict. The next run tries again.

A description that starts with the legacy text `Synced from Kea DHCP` is also a marker. The legacy marker is that
text followed by the longest known kind that matches (` lease + reservation` before ` lease`). The kind counts only
when the text after it is empty or starts with a character that is not a letter, a digit, `-` or `_`. Otherwise, as
in `Synced from Kea DHCP leases`, the legacy marker names no kind and all text after `Synced from Kea DHCP` is kept.
The next write that rewrites the description replaces the legacy marker with the block, and keeps the text after it.
For example, `Synced from Kea DHCP lease my note` becomes `[kea-sync: lease] my note`. Adoption (see Upgrade) uses
the same recognizer.

A blank description is not the marker. When a run reports an object that has no link and no marker, the run
reports a conflict and does not change the object. Only a forced `claim` writes the marker over it and links it.

Several Servers can own one object, for example the two members of a Kea HA pair. A run compares the facts that its
phase reports with the facts stored on the live links of the other owners. A link is live when it has facts and no
stale mark. All owners compare the prefix length. Only owners of the same source compare the hostname, and an empty
hostname makes no claim: a lease and a Reservation of one address often name the host differently, for example
`printer.example.com` and `printer`. The hostname of a live Reservation link wins, so a lease does not change the
DNS name of such an object. When the facts differ, the object keeps its current facts, the run stores its own facts
on its link, and it reports an owner disagreement. One phase compares two reports of one object with the rule for
owners of the same source: the prefix lengths must be equal, and an empty hostname makes no claim. When the reports
agree, the phase applies the non-empty hostname. When they disagree, for example two Reservations in overlapping
Subnets, the run does not create or change that object, and does not change the facts that an existing link of that
owner stored before. It reports an owner disagreement. The phase still reports the object, so it keeps a link to it
(see Stale objects). When the disagreeing link goes, the cleanup sets the hostname that the remaining live links
imply (see the status below), and the next run of a remaining owner applies its other facts.

The status of an IP address comes from the live links of all its owners: `active` (kind `lease + reservation`) with
at least one lease link and one Reservation link, `dhcp` (kind `lease`) with lease links only, and `reserved` (kind
`reservation`) with Reservation links only. The links of all Servers count, so two Servers that own one object, one
through a lease and the other through a Reservation, do not change its status on each run. A run sets the status
when it applies a report, and when a cleanup removes a link that is not the last one. That cleanup also sets the
hostname that the remaining live links imply: the hostname of a live Reservation link wins, else that of a live
lease link. An empty hostname, or live links of that source that name different hosts, change no DNS name. A report
that the run does not apply, after an owner disagreement or for a Global Reservation, leaves the object unchanged,
its status included. An object without a live link keeps its status.

### Identity

Lease and Reservation IP addresses use `Server.sync_vrf`, as Prefixes and IP Ranges already do. Lookup and
creation are scoped to that VRF. The only exception is the adoption in Upgrade, which also looks up the global VRF.

### Stale objects

A Stale IPAM Object is an owned object that a complete phase of its owner no longer reports. A phase is complete
when its snapshot is complete and no row in the phase failed. Each complete phase removes only its own stale links.
Each link records a confirmation number that a run takes when it confirms the link. The phase takes its cutoff
number before it requests the snapshot from Kea, and removes a link only when its confirmation number is lower
than the cutoff. A claim that confirms the link after the cutoff therefore keeps it. Both numbers come from one
PostgreSQL sequence with a cache of 1, taken when the statement runs. Sequence values grow in the order of the
calls across all sessions, and a clock adjustment cannot change that order. `reconcile` runs the claims of all its
phases before it removes any link, so an object that moves from one source to another in one run keeps its ID.

The lease and Reservation snapshots come from separate Kea commands. An address that moves from one source to the
other between the two reads can be absent from both, and the run then treats it as stale. The next run creates it
again with a new ID. Kea has no snapshot that covers both sources, so this ADR accepts that window.

A `claim` can also wait for the identity lock while cleanup holds it. When cleanup removes the object in the
`remove` mode, the claim then finds no row and creates the object again with a new ID. The snapshot of the cleanup
phase did not report the object, so the removal is correct for that snapshot. This ADR accepts that window too.

A phase links every existing owned object that it reports, also when it does not apply its report: after a
same-phase owner disagreement, and for the address of a Global Reservation. ADR 0002 does not synchronize Global
Reservations, so the Reservation phase never creates or changes their objects, but it links an existing one. When
the phase has no facts to store, the link has none. A link without facts takes part in no fact comparison and no
status computation, but it is a link for cleanup. Every report is therefore a stored link with a confirmation
number, and a concurrent phase sees it under the object lock.

A phase removes the last link of its Server to an object only when the `reconcile` call runs every phase that can
report that object type, and each of them is complete. For an IP address these are the lease and the Reservation
phases. If `Server.sync_reservations_enabled` is false, the Reservation source is not an owner for that Server.
A complete lease phase alone can then remove its last lease link. This exception does not apply to the global
Reservation toggle or to an unavailable `host_cmds` hook. Config-file Reservations can still exist without the
hook, so that Reservation phase stays incomplete and the last link stays. A failed or truncated lease snapshot
keeps every stale lease link. Otherwise the link stays and a later run decides. A Prefix is the exception: the job reports it from the
Subnet and `lease-prefix` phases and the DHCP plugin import from the `delegated-prefix` phase, so each of these
phases counts alone. A Prefix is never removed, so the worst case is an opt-in deprecation that the next Subnet
run reverts when it links the Prefix. A bulk Reservation Sync has no complete lease phase,
so it never removes the last link of its Server, even when that Server disables Reservation sync. A failed phase does not block the
removal of a link when the Server keeps another link to the object.

When a complete phase removes the last link of a Stale IPAM Object, the object changes as follows:

- An IP address follows the plugin setting `stale_ip_cleanup`: `remove` (default), `deprecate` or `none`.
- A Prefix or IP Range is never removed. A per-Server field, off by default, lets the operator opt in to
  deprecating it. The field of the Server whose phase dropped the last link applies. Operators often attach
  site, VLAN or tenant data to these objects. When the field is off, the link goes and the object stays
  unchanged.

A release and the deletion of a Server also remove links, but they never remove or deprecate the object.

When a deprecation applies, the last link stays and is marked stale, so the object returns to its computed
status when Kea reports it again. A stale link takes part in no fact comparison and no status computation. The
mark goes when its owner reports the object again and the run applies that report. The link goes when another
owner links the object, unless its own owner confirmed it after the mark. A confirmation without an applied
report, as after an owner disagreement, keeps the mark and does not restore the status.
The DHCP plugin reference guard stays: an object that the DHCP plugin references is never removed or deprecated.

Before issue 294, the `lease` source owned the prefix base of a delegated prefix as an IP address. A `lease` link records
its allocation kind; a DHCPv6 link from before that change has none. A complete `lease-prefix` phase that links the
Prefix of a delegated prefix classifies the old link at its base, and the normal `lease` cleanup then retires it.
An address lease that a complete `lease` phase reads, current or not, classifies its link as an address link. No cleanup removes an unclassified link, including
the removal of a stale link that another owner supersedes; a release still removes it.

Deleting a Server drops its links. An object without an owner becomes an unowned marker object. The
synchronization never cleans it, and the job summary counts it.

### Interface

One IPAM Reconciliation module owns ownership, cleanup, per-row savepoints and conflict counting:

```text
reconcile(server, family, phases) -> SyncReport       # complete phases, with cleanup
claim(server, family, records, force) -> ClaimResult  # one or more records, links, never cleans up
```

The job and the bulk views call `reconcile`. The per-row Sync, lease add and the DHCP plugin import call `claim`.
The records of one `claim` call count as one phase for the fact comparison, so the DHCP plugin import passes all
records of one snapshot in one call. The DHCP plugin import also calls `reconcile` for its `delegated-prefix`
phase. The job reads one Lease Snapshot per family, with one cutoff, for its `lease` and `lease-prefix` phases.

Before `claim` and `reconcile` look up or create an object, or change its links, they take a transaction-level
PostgreSQL advisory lock on the object identity: the VRF and the address, Prefix or IP Range. They decide the
last-link change under that lock. Two runs therefore cannot both create one object, and of two runs that drop the
last two links of one object, the second sees that no link is left. Under the advisory lock, they also lock the
row of an existing object (`SELECT ... FOR UPDATE`) and read its description from that row before they link,
change, deprecate or remove the object. An operator edit that removed the marker and committed first is therefore
seen, and an edit that commits later waits for the row lock. A lock error fails only that row, so the phase is not
complete.
This decision lands in one change with that module.

### Upgrade

No data migration guesses owners. The first complete run of each Server links the marker objects in its
keep-set. It does not adopt an object with a blank description: the sync writes the marker on every object that it
creates or updates, so the sync did not write a blank object last. A Server with a non-global `sync_vrf` looks up
each IP address of its keep-set in its `sync_vrf` and in the global VRF. It holds the advisory lock of both
identities, the global VRF first. For a marker IP address in the global VRF, the rule depends only on the
`sync_vrf` of all Servers, never on which job runs first:

- A Server whose `sync_vrf` is the global VRF adopts the row in place.
- When every Server has the same non-global `sync_vrf`, the first run moves the row into that VRF, unless another
  Server already owns it. The object keeps its ID and changelog, and every later run finds it in that VRF. When
  that VRF already has an IP address with the same address, the row does not move. It stays unowned and counted,
  the run reports a conflict, and the row in that VRF follows the normal rules.
- Otherwise the row is ambiguous: Servers with different VRFs could each report it. A Server with a non-global
  `sync_vrf` does not move or adopt it, and creates its own row in its own VRF.

Adoption marks the links that it creates, and a link that a run creates for an object with a marked link is marked
too. Each Server records when its first complete run after the upgrade finished. Until every Server that
synchronizes IPAM has finished that run, a phase does not remove the last link of an object with a marked link, so
an owner that is not yet known cannot lose the object. A run is complete when every phase that the Server enables
is complete. A family or source that a Server does not synchronize makes that Server no owner, so the barrier
does not wait for it. The job summary counts the objects that wait for this.

Marker objects that no Server adopts stay unowned, and the job summary counts them for explicit handling by the
operator.

### Guards

- An OpenGrep rule refuses `.delete()` and status changes on `ipam` IP addresses, Prefixes and IP Ranges
  outside the reconciliation module.
- A regression test has two Servers, one hostname, and overlapping address space in two VRFs.

## Consequences

Cleanup cannot cross Servers or VRFs. The rows of expired leases, deleted Reservations and hostless leases now
become stale. With the default `remove` mode, the row of a lease that expires after the upgrade is removed. A row
that was already stale before the upgrade gets no link, so it stays unowned and counted.

The synchronization no longer adopts an object with a blank description. An operator who creates an IP address,
Prefix or IP Range in NetBox before Kea reports it forces one `claim` for it, or writes the marker.

The hostname index and the moved-device rule are deleted. The description marker stays as the release signal
for operators, not as the ownership record.

Prefixes and IP Ranges get ownership for the first time. An upgrade does not change them: deprecation is opt-in
per Server, and the synchronization never removes them.

## Rejected alternatives

- Keep the moved-device rule and scope it to the owner: rejected because the rows of expired leases stay
  forever.
- A NetBox tag or a custom field per Server: rejected because neither has constraints, and a custom field
  holds one owner.
- A generic foreign key, as `KeaDhcpLink` uses: rejected because core models cannot get a `GenericRelation`,
  so a deleted object leaves a dangling link.
- One link table per object type: rejected because the cleanup logic is then written three times.
- One owner per object: rejected because a Kea HA pair then synchronizes through one member only.
- Ownership per Server without a source: rejected because one failed phase then blocks all cleanup.
- The link as the only ownership fact: rejected because an operator could no longer protect a curated object
  by editing its description.
- Last writer wins when owners disagree: rejected because the object changes on every run.
- Read the current reports of all owners during a run: rejected because one run then calls the Kea API of other
  Servers, and an unreachable Server then blocks the run of another.
- A data migration that assigns owners: rejected because overlapping Subnets make it a guess.
- Confirmation times from the PostgreSQL clock (`clock_timestamp()`): rejected because the clock can step back,
  and a claim made after the cutoff then gets an earlier time and loses its link.
- Let cleanup detect a `claim` that waits for the identity lock: rejected because a claim that starts just after
  the removal commits gets the same new ID, so the window only gets smaller.
- Move a legacy global-VRF row into the `sync_vrf` of the first Server that adopts it: rejected because the row's
  ID and changelog then depend on which job runs first.
- Apply the cleanup mode when a Server is deleted: rejected because deleting a Server must not delete IPAM data.
- Deprecate stale Prefixes and IP Ranges by default, or allow their removal: rejected because operators attach
  their own data to these objects, so any change to them must be an explicit per-Server choice.
- One entry point with record and force arguments: rejected because ADR 0001 and ADR 0003 prefer purpose-named
  operations to mode flags.
