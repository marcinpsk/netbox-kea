<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba
SPDX-License-Identifier: Apache-2.0
-->

# Lease state compatibility fallback

## 0. Review state

- The first implementation review confirmed that assigned and declined counts
  cannot bound every row returned by `lease-get-all`.
- Reopen that claim if Kea adds an authoritative current database-row count to
  the supported statistics response.

## 1. Brief

### Decision

Choose the compatibility behavior when `lease4-get-by-state` or
`lease6-get-by-state` is unavailable.

### Ownership and seam

`KeaClient` owns lease-query safety. The seam is the transition from the
subnet preflight in `_subnet_lease_search_spec()` to command execution in
`_lease_search_response()`.

### Constraints

- Kea before version 3.1.5 does not provide the state command.
- `lease4-get-all` and `lease6-get-all` return all database leases for a selected
  subnet and can produce a large response.
- `stat-lease4-get` and `stat-lease6-get` report assigned and declined counts.
  Those counts do not include every retained lease state in the database.
- Expired-reclaimed leases can remain in the lease database for the configured
  hold period.
- An absent query guard or unavailable measurement must fail closed.
- Supported state commands must remain subnet-scoped and state-scoped.

### Observable acceptance conditions

1. A missing state command never starts an unpaged request based on a count that
   excludes rows that the request can return.
2. A supported state command still returns the requested state for the selected
   subnet.
3. The transport trace proves the unsafe command is absent on every fail-closed
   path.
4. A regression case includes a small assigned count and many retained leases.
5. The implementation does not maintain two compatibility paths.

### Evidence

- ISC documents `lease4-get-all` and `lease6-get-all` as returning all leases for
  the selected subnets.
- ISC documents expired-reclaimed leases as remaining in the lease database for
  a configured hold period.
- The `stat-lease4-get` response contains assigned and declined counts, but it
  has no current-row count for expired-reclaimed, released, or registered states.
- The read-only adversarial probe demonstrated that the current candidate marks
  a subnet as bounded from one assigned lease and then accepts a response with
  1,001 rows.

## 2. Candidate shapes

Two designs were produced before comparison. The primary designer used this
repository context with GPT-5 high reasoning. The blind designer used a fresh
GPT-6 Astra high, read-only context. Its packet contained only the facts and
acceptance conditions in section 1. It did not contain the primary design, this
record, or prior reviewer output.

### A. Scoped state query or explicit failure

Keep the public `lease_search()` interface. Send only the subnet-scoped state
command. If Kea reports that the command is unsupported, raise
`LeaseQueryPreflightUnavailable("state-command")`. Delete the `get-all`
compatibility branch, its authorization boolean, and fallback-only filtering.

### B. Scoped state query with bounded global pagination

If the state command is unsupported, scan global lease pages with a finite
record budget. Count every row, including rows for other subnets and other
states. Return a result only after the scan completes without truncation.
Otherwise fail closed.

Both designs select candidate A. It keeps capability and failure policy inside
`KeaClient`. It does not make callers manage a scan budget. Deleting the
fallback removes more mechanism than it adds. Candidate B changes a subnet
query into a daemon-wide operation and can reject a small subnet only because
an unrelated subnet grows.

## 3. Divergence table

| Decision | Primary design | Blind design | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Unsupported state command | Raise the existing compatibility error | Same | No available statistic bounds every row returned by `get-all` | Select explicit failure | Older Kea cannot complete a state-filtered search |
| Paged compatibility scan | Reject because the command is global | Reject because it needs a new budget and changes query locality | The page command has no subnet filter | Do not add it | The search stays one scoped request or one explicit error |
| State-free subnet search | Fix only with a separately ratified query design | Record as a separate reachable instance | It still uses assigned counts to authorize subnet `get-all` | Exclude from this narrow fallback revision and report as open work | The existing state-free guard remains unchanged in this change |

The merged revision is **r1**: select candidate A for the missing state-command
path. The mechanical guard is the absence of an alternate retrieval command in
that error path, verified by exact transport traces for guarded and unguarded
clients.

## 4. Ratification rounds

### Round 1

The Astra-high read-only reviewer returned:

`RATIFY r1, missing state-command compatibility scope`

It closed all five acceptance predicates. It reproduced the unsafe `get-all`
call for DHCPv4 and DHCPv6 with one assigned lease and 1,001 returned rows. It
also reproduced the guarded, unguarded, and supported-command controls.

The reviewer accepted the state-free subnet-search disposition as separate from
r1, but confirmed that underlying issue remains open.

## 5. Verdict and first increment

Revision r1 is ratified for the missing state-command compatibility scope.

The first increment adds the retained-row regression and observes the existing
fallback call. It then removes the fallback, its authorization boolean, and its
fallback-only filtering. Focused tests must prove that unsupported commands fail
closed and supported commands keep exact subnet and state arguments.

Open work: the state-free subnet query still uses assigned counts to authorize
`lease-get-all`. It needs its own query-design decision because the only existing
paged command scans all subnets.
