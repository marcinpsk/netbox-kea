<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Lease claim fact authority

## Scope and acceptance

IPAM Reconciliation owns the facts stored on ownership links. Its `claim` and `reconcile` interfaces must resolve the mask of one lease from the same authority. The per-row lease Sync and lease add use `claim`. Reservation records already carry a verified Subnet mask. This decision implements the lease claim contract of ADR 0006.

With two Servers reporting the same Kea `/24` lease and no NetBox Prefix, both links must store `/24`. Neither report may cause an owner disagreement. A larger or wrong-VRF NetBox Prefix must not override available Kea facts. Unknown and unavailable Subnet facts must have explicit failure behavior. Empty and Reservation claims must make no new catalogue request. Existing ownership locks, confirmations, release rules, row failure isolation and the no-cleanup contract remain.

## Evidence and prior findings

The initial claim implementation inferred the mask from a NetBox Prefix, then used the host mask if no Prefix existed. The job already read the Kea catalogue and passed its Subnet masks to reconciliation. Thus a claim could store `/32` while another owner correctly reported `/24`. This artificial disagreement could block a later update from that owner. Existing concurrency fixtures created a matching Prefix and did not expose this case.

The old per-row helper also inferred a mask, but it did not persist that inference as another Server's ownership facts. The new obstruction was confirmed during adversarial review. The earlier claim that the fallback introduced no behavioral defect is reopened by this reachable input. No finding about this mask authority is refuted.

## Decision (revision r1)

Keep `claim(server, family, records, force)`. IPAM Reconciliation reads the Subnet Catalogue once for a nonempty lease claim. Empty and Reservation claims need no such read.

One private lease report builder serves both claim and reconciliation. It receives the Server, family, lease record and Subnet-ID mask authority. A mapping, including an empty mapping, means the Kea catalogue was available. The lease Subnet ID must exist in that mapping. `None` means the job explicitly selected its existing fallback after the catalogue read failed. This type distinction prevents an unknown ID from silently becoming a guessed mask.

For available authority, the builder uses the exact Subnet mask. For the job's explicit fallback, it uses the longest containing Prefix in `Server.sync_vrf`, then the host mask. The independent claim lookup and reconciliation's legacy unscoped mask resolver are removed from this flow. Legacy callers outside reconciliation retain their existing implementation until their replacements land.

`LeasePhase.subnet_prefix_lengths` permits `None`. The job passes a mapping after a successful catalogue read and `None` only after its existing unavailable-catalogue handling. An available empty catalogue differs from an unavailable catalogue.

Claim validates and groups every report before ownership writes. An unavailable catalogue raises `CatalogueUnavailable`. An unknown Subnet ID raises a validation failure before any claim record is written. Both lease HTTP callers handle these failures and show a generic error. Lease add retains the successful Kea operation and reports the IPAM failure. Force permits an ownership takeover; it cannot bypass fact authority.

Reconciliation counts an unknown Subnet ID as a row error and marks the lease phase incomplete. Its existing cleanup protection then applies. A valid row can proceed. Object identity locks, row locks and confirmation allocation remain in the existing claim machinery.

## Blind design comparison

The coordinator and an independent agent received the same factual brief. The independent agent started with a fresh context and did not receive the coordinator's proposal. The coordinator used Codex. The other agent inherited the session model and reasoning effort; those values were not exposed to it.

| Decision | Coordinator proposal | Independent proposal | Disposition and consequence |
| --- | --- | --- | --- |
| Fact owner | Internal catalogue read and shared builder | Same | Retain. Fact authority stays local to IPAM Reconciliation. |
| Unavailable claim catalogue | Per-address error results | Raise `CatalogueUnavailable` before writes | Use the exception. The caller reports failure for the complete observation and no guessed link is stored. |
| Unknown ID in a claim | Fail that address | Validate all reports before writes | Use whole-call validation. It preserves the existing validation-before-write contract. |
| Available versus unavailable job authority | Keep an explicit fallback | Mapping versus `None` | Use the type distinction. An available empty catalogue cannot select fallback. |
| Typed facts supplied by callers | Alternative only | Alternative only | Reject for this scope. It expands the caller interface and duplicates authority responsibilities. |

Deleting the private builder would restore mask policy in two readers. The builder provides locality and keeps the public interface small. Reusing caller-specific observations can be considered only when evidence requires it; no additional context argument or configuration flag is added.

## Validation and next increment

First add the failing real HTTP regression with a second Server's `/24` ownership link and no NetBox Prefix. Then implement the shared builder and catalogue read. Verify the later owner update, available empty catalogue, unknown IDs, unavailable catalogue, wrong-VRF covering Prefixes and calls that require no catalogue. Tests use the real ORM and KeaClient, with only the HTTP transport stubbed. Retain real PostgreSQL claim/reconcile race tests.

Adversarial review ratified revision r1 against the acceptance conditions above, with no unresolved design blocker. The first HTTP regression failed on the persisted ownership facts: the claim stored `/32` while the Kea Subnet and existing owner reported `/24`. Implementation starts after this ratification and red proof. The complete change then receives Standards and Spec review, a full native test run and coverage verification before push.

The shared builder correction passed the HTTP regression and 425 focused tests, including the job and real lock races. Focused coverage executed all 118 changed production statements. Missing-authority and unknown-ID tests verify refusal without guessed ownership writes.

A subsequent boundary regression exposed malformed Subnet IDs that could abort the Server run. The builder now validates the integer type and existing Kea bounds before lookup or fallback. Valid leases and Reservations continue after a malformed lease. The focused suite passed 429 tests and covered all 121 changed production statements.
