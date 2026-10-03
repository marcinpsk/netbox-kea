<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Throwaway DHCP Import Mapping prototype

PROTOTYPE. This branch is evidence, not a production implementation. Do not merge it.

## Question and answer

Can native branch history restore the association between an imported DHCP target and its Kea source?

Yes, for the tested dependency-aware squash lifecycle. Original target and mapping primary keys survive
revert. Reimport updates the restored target without creating a duplicate. The observed result covers Subnets
and Global Reservations, IPv4 and IPv6, instance and queryset deletion. Discard preserves main.

Installed iterative replay tries to restore the generic mapping before its deleted target exists. The
unguarded control failed with ObjectDoesNotExist. Affected mapping deletions therefore need squash. The
prototype refuses iterative merge before mutation and leaves the selected strategy unchanged.

## Run and inspect

Use a throwaway NetBox instance with netbox-branching 1.2.1, netbox-plugin-dhcp 0.2.0 and PostgreSQL.
Use the native isolated runner, configured for that instance, from this checkout:

```sh
kea-testdb run --create-db netbox_kea/tests/test_prototype_dhcp_mappings.py -q
```

The recorded run used the task-owned kea-cr288-branch instance and kea-cr288 Docker network. The wrapper
provides an isolated database set and Redis. Do not point this experiment at a production NetBox instance.
Open lifecycle.html for a mobile-sized illustration of the observed squash states. The illustration is not
a substitute for database tests and does not simulate concurrency.

## Recorded evidence, 2026-10-03

The instance ran NetBox 4.7.0, Python 3.14 and Django 6.1. No compatibility-matrix claim is made.

| Stage | Actual result | What it establishes |
| --- | --- | --- |
| Main-only baseline | 1 failed | Revert restores the target but not its mapping. |
| Branchability without guards | 6 passed, 8 subtests | Squash lifecycle works; iterative missing-target failure, synchronous replay provenance and old-schema fallback were observed controls. |
| Safety requirements without guards | 2 failed | A real main import after preflight loses its new mapping during merge. An old-schema branch target deletion reaches main's mapping table. |
| With transaction and schema guards | 14 passed, 16 subtests | Both unsafe controls now refuse and preserve main; squash lifecycle, discard and strategy refusal still work. |
| Preserved iterative control | 1 passed (expected target DoesNotExist) | With the prototype preaction receivers disconnected, native revert reaches the missing Subnet dependency. |

The final suite repeats six inherited lifecycle methods in the safety class. Its 14 test count is not 14
independent scenarios. The source of the baseline recovery regression is preserved in baseline-recovery.patch.
Apply it to this branch's original base with `git apply --unidiff-zero baseline-recovery.patch`, before the
prototype edits, to reproduce the main-only failure.
The probes use real ORM, PostgreSQL, importer, native merge/revert and change logging. Only external Kea
HTTP transport is stubbed for the Reservation observation fixture.

Run the preserved strategy control with `-q -rP` to see its actual exception. Its output was:

```text
PROTOTYPE_RESULT iterative restoration: DoesNotExist: Subnet matching query does not exist.
ERROR netbox_branching.branch.revert: Subnet matching query does not exist.
1 passed in 252.64s (0:04:12)
```

This is an expected-failure control with real branch operations, not an injected exception. It retains save/delete guards, explicitly clears native request history, and restores
the preaction receivers in finally. Those receivers also perform schema inspection; the control uses a fresh branch. The separate guarded iterative test verifies refusal before mutation.

The late-import probe pauses native pre_merge in another thread, imports on main through the real importer,
then resumes merge. It asserts refusal and preservation of both the main target and newly attached mapping.
The guard validates reversible coverage inside the deleting transaction, not only during preflight.

Raw mapping ORM queries still fall through to main when an old branch lacks the table. That remains an
intentional diagnostic control. A prototype guard in the drift reader demonstrates schema inspection, but
its complete public HTTP message and all read surfaces have not been validated.

## Limits and production gates

- The advisory lock is a throwaway global lock. Lock ordering across every metadata writer, Server cascade,
  main target edit, import, merge and revert is not proved. Nontransactional target saves are not covered.
- Native AppliedChange provenance is observed synchronously in main pre_delete. Native signal history is
  cleared at the prototype preaction boundary, matching the existing tracked-job pattern. Production must
  validate all real request/job boundaries and success/error context cleanup.
- A newer-main revert conflict, reused target identity, missing Server and complete cross-object rollback
  matrix remain implementation work. Passing the late-import refusal does not establish all conflicts.
  In particular, reverting a branch-created Global Reservation can delete a mapping that main import
  attached after merge. The prototype's deletion coverage guard checks merge only. The late-import probe
  also has no earlier unrelated mutation and does not prove whole-operation rollback or status cleanup.
- Timestamp-only Global Reservation imports still use update_or_create and can produce change history.
  Production must separate observation time from semantic mapping identity and prove no-op behavior.
- Configuration exclusions, optional-plugin absence, dry runs and the public old-branch view message need
  complete behavioral coverage. The prototype does not establish them.
  The current template ignores mapping_unavailable. A provisioned table also does not establish safe
  routing when configuration exempts the mapping model. Production must check both schema and routing.
- Model validation in the prototype is partial. Production must enforce the supported target scope and
  source/family identity at its real boundaries.
- Existing main-only guards in the ordinary suite deliberately describe ADR 0007 and will need changes in
  the complete feature. This prototype passed its focused probes, not the production full suite.
- No old-schema/history retrofit or repair of already-lost mappings is attempted.

Keep the established main-only policy for Server, settings, imports, live Kea and IPAM ownership. Implement
the agreed exception as one complete change with real regressions and adversarial review. Do not copy these
throwaway signal handlers into production without resolving the gates above.

The read-only high-reasoning Astra review approved capture as bounded evidence and specification publication.
It did not approve production readiness. See adversarial-review.md for its concrete failure scenarios.
