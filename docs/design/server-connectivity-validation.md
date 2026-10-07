<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Server submission connectivity validation

## Section 0: settled and refuted claims

- CSV absent-field errors are CONFIRMED on 4.3. A native Django proof raises ValueError for a missing dhcp4 field. Native 4.7 import already remaps these errors; the shared adapter must preserve that behavior and supply the same safeguard at the floor.
- Bulk transaction rollback is REFUTED as a leak: both native versions wrap the updater in atomic and clear events on propagated ValidationError. Reopen if checks move outside atomic, errors are swallowed or nontransactional receivers are introduced.
- Initial bulk selection is REFUTED as unrestricted: native permission mixins restrict the view queryset. Reopen if implementation uses the form PK queryset or an unrestricted manager.
- Candidate permission changes need an early check. A dhcp4=True permission constraint can fail after a bulk change to False. The outer native permission check occurs after the updater returns, so repeat it before probes.
- Native import clean return values differ across supported releases. A real 4.3 CSV update raises AttributeError before connectivity validation when ServerImportForm treats super().clean() as a mapping. Call the native method, then use the form's canonical cleaned_data to retain omitted-column defaults.


## Decision brief

Server model validation performs live Kea reads, so an outage blocks metadata edits through native forms, REST, CSV and bulk edit. Model validation must remain local. The submit adapters must reject unreachable enabled services when creating a Server or changing connection fields, preserve existing field errors and protocol routing, and permit unchanged connection values and metadata edits without HTTP.

The public seams are Server.full_clean and real Server UI add/edit, REST POST/PATCH/PUT, CSV import/create/update and bulk-edit POST. Use real ORM and KeaClient, with only external Kea transport stubbed. Check both supported API endpoints, NetBox 4.3 and 4.7.

Candidate shapes are a shared connection policy with submit hooks and native bulk updates, or a shared policy with restricted bulk preflight. Avoid network in model signals, model flags and context variables.

## Independent designs

The coordinator designed a policy that fetches persisted original values and uses restricted bulk preflight. GPT-6.1 Sol, high reasoning, independently designed value snapshots before native mutation and validation of native bulk return values before transaction commit. The second designer received the problem, repository evidence and acceptance only. It did not receive the coordinator proposal. The coordinator proposal was drafted in analysis before the complete second proposal arrived, then archived during native hook research.

## Divergence table

| Decision | Coordinator | Independent designer | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Original values | Read persisted values in shared policy | Capture before native mutation | Both native serializers mutate self.instance; native bulk writes before returning | Adopt explicit before-value snapshot | No extra policy database query; caller captures before state once |
| Bulk check point | Preflight copied flags before native writes | Check native updated objects inside atomic, before commit | 4.3 has no pre_save_operations; both versions wrap _update_objects in atomic and catch ValidationError | Candidate: native bulk update and transaction-local check, subject to ratification | Avoid repeated native mutation mapping; failed probes must roll back rows, tags and ObjectChange records |
| Bulk form preflight | Reject because selection needs restricted view queryset | Reject for same reason | Bulk form PK queryset is not the restricted view queryset | Reject | Probe only native selected/updated objects |
| Unused family settings | Any actual connection change probes enabled families | Same | Ticket calls these connection fields; no narrower endpoint policy selected | Adopt | Consistent simple rule across all adapters |
| Prevention guard | Zero-HTTP full_clean regression | Optional narrow AST guard plus behavior test | Entire Kea transport remains centralized | Use behavior regression | No maintenance-heavy analyzer for one model boundary |

## Candidate r2

A server_connection module owns CONNECTION_FIELDS, connection_values(server), and validate_connection_change(server, before). A None baseline means creation. Otherwise compare normalized values. Probe each currently enabled family only on creation or actual change. Preserve the native get_client routing and existing ValidationError fields/messages, including malformed JSON handling.

ServerForm and ServerImportForm share a ModelForm _post_clean adapter. Capture existing connection values before native instance construction, or None for adding instances. Run native local validation first. Probe only if no form errors. Translate Django ValidationError with native ModelForm error machinery. Present fields retain their field errors. If a CSV update omits the family field, remap that error to a non-field row error and retain its family field label. This must work on 4.3, not only with the native 4.7 remapping.

ServerSerializer captures its existing values before super.validate, which mutates self.instance on both native releases. Use that effective instance on update. For creation build only connection fields from validated data and model defaults. Native validation handles local fields and M2M separately. Preserve nested serializer behavior and null-to-empty coercion. Translate Django errors to native DRF field errors.

ServerBulkEditView captures connection values for its restricted selection before delegating _update_objects. Before any connectivity probe, repeat the native updated-PK permission count against the restricted view queryset. Raise native PermissionsViolation if the proposed state no longer meets its object permission constraints. Preserve the outer native check. Then validate returned native updated objects before returning, inside the existing transaction. Any failed connectivity check must raise Django ValidationError, abort the full batch, and leave rows, tags and ObjectChange records unchanged. Native queue clearing must suppress mutation-event dispatch on failure. Do not copy native mutation logic. No network occurs during model clean, so all local validation completes before these probes.

## Observable acceptance

- Server.full_clean sends zero Kea requests for valid create/update instances and still rejects invalid local settings.
- Real UI and REST creates or connection edits reject unreachable enabled services with existing field errors.
- UI, REST, CSV and bulk metadata edits and explicitly unchanged connection values succeed with zero HTTP.
- CSV creation preserves omitted defaults and checks the resulting enabled families.
- Failed bulk connection checks restore every selected row, tag and change record. No denied/unselected Server is probed.
- Disabling one family checks only the remaining enabled families. Direct-daemon and Control Agent requests retain URL/service semantics.
- Real focused regressions pass on NetBox 4.3 and 4.7. The final full suite and independent Standards/Spec review pass before publication.

## Ratification evidence and next action

Astra r1 ratified the shared policy and bulk transaction mechanism, but returned NEEDS-WORK for 4.3 CSV absent-field error mapping. That blocker is CLOSED in r2 by the explicit row-error fallback, subject to reviewer acceptance. Read-only Sol also proved the candidate permission-state case and its native count check. R2 adds that check before all probes.

Verbatim changes for r2:

1. Remap connectivity errors for omitted CSV family fields to non-field row errors, preserving the family label. Keep present-field errors and native form error handling.
2. Repeat the native updated-PK permission check after native bulk updates and before every connectivity probe. Preserve the outer native check. Add a real constrained-permission bulk regression.

Astra high RATIFY r2, full scope. Both design blockers are CLOSED. The reviewer retained Section 0 reopening conditions. Production edits began after ratification.

Real model validation and metadata-edit POST regressions failed before the production change. REST creation and failed bulk-batch regressions also failed before their adapters were added. The bulk regression checks row, tag and ObjectChange rollback, with no mutation events. A successful control proves event tracking is active.

The real 4.3 CSV update first exposed the native clean return-value mismatch. After that fix, the same request raised ValueError for an absent dhcp4 field. The shared error adapter maps absent fields to labeled row errors. The proposed-state permission regression also failed when only the early count check was removed: it sent version-get before native permission denial. The restored check rejects the candidate before any Kea request.
