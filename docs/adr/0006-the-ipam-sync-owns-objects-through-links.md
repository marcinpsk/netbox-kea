---
status: accepted
date: 2026-09-27
---

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
requires exactly one. The source is `lease`, `reservation`, `subnet`, `pool` or `delegated-prefix`. The link is
unique per `(server, family, source, object)`. Each link also stores the facts that its owner last reported for
the object.

An object is owned when it has at least one link and its description still starts with the marker. An operator who
removes the marker from the start of the description releases the object. A note after the marker does not. The
next run drops the links and reports a conflict. The `lease + reservation` status comes from the links of the
object.

Several Servers can own one object, for example the two members of a Kea HA pair. A run compares the facts that its
phase reports with the facts stored on the other links of the object. When they differ, the object keeps its
current facts, the run stores its own facts on its link, and it reports an owner disagreement. When one phase
reports one object twice with different facts, for example two Reservations in overlapping Subnets, the run does
not create, change, link or unlink that object. It reports an owner disagreement, and an existing link of that owner keeps
the facts it stored before. When the disagreeing link goes, the next run of a remaining owner applies its facts.

### Identity

Lease and Reservation IP addresses use `Server.sync_vrf`, as Prefixes and IP Ranges already do. Lookup and
creation are scoped to that VRF.

### Stale objects

A Stale IPAM Object is an owned object that a complete phase of its owner no longer reports. A phase is complete
when its snapshot is complete and no row in the phase failed. Each complete phase removes only its own stale links.
Each link records when a run last confirmed it, and a phase removes a link only when that time is before the phase
read its snapshot. A claim made after the snapshot therefore survives. A failed phase does not block cleanup of
another phase. `reconcile` runs the claims of all its phases before it removes any link, so an object that moves
from one source to another in one run keeps its ID.

When the last link of an object goes, the object changes as follows:

- An IP address follows the plugin setting `stale_ip_cleanup`: `remove` (default), `deprecate` or `none`.
- A Prefix or IP Range is never removed. A per-Server field, off by default, lets the operator opt in to
  deprecating it. The field of the Server whose phase dropped the last link applies. Operators often attach
  site, VLAN or tenant data to these objects. When the field is off, the link goes and the object stays
  unchanged.

When a deprecation applies, the last link stays and is marked stale, so the object returns to its computed
status when Kea reports it again. A stale link takes part in no fact comparison and no status computation. The
mark goes when its owner reports the object again, and the link goes when another owner links the object.
The DHCP plugin reference guard stays: an object that the DHCP plugin references is never removed or deprecated.

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
phase, because no other caller reports delegated prefixes.

Before `claim` and `reconcile` look up or create an object, or change its links, they take a transaction-level
PostgreSQL advisory lock on the object identity: the VRF and the address, Prefix or IP Range. They decide the
last-link change under that lock. Two runs therefore cannot both create one object, and of two runs that drop the
last two links of one object, the second sees that no link is left. A lock error fails only that row, so the phase
is not complete.
This decision lands in one change with that module.

### Upgrade

No data migration guesses owners. The first complete run of each Server links the marker objects in its
keep-set. For a marker IP address in the global VRF, the rule depends only on the `sync_vrf` of all Servers, never
on which job runs first:

- A Server whose `sync_vrf` is the global VRF adopts the row in place.
- When every Server has the same non-global `sync_vrf`, the first run moves the row into that VRF, unless another
  Server already owns it. The object keeps its ID and changelog, and every later run finds it in that VRF. When
  that VRF already has an IP address with the same address, the row does not move. It stays unowned and counted,
  the run reports a conflict, and the row in that VRF follows the normal rules.
- Otherwise the row is ambiguous: Servers with different VRFs could each report it. A Server with a non-global
  `sync_vrf` does not move or adopt it, and creates its own row in its own VRF.

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
- Move a legacy global-VRF row into the `sync_vrf` of the first Server that adopts it: rejected because the row's
  ID and changelog then depend on which job runs first.
- Apply the cleanup mode when a Server is deleted: rejected because deleting a Server must not delete IPAM data.
- Deprecate stale Prefixes and IP Ranges by default, or allow their removal: rejected because operators attach
  their own data to these objects, so any change to them must be an explicit per-Server choice.
- One entry point with record and force arguments: rejected because ADR 0001 and ADR 0003 prefer purpose-named
  operations to mode flags.
