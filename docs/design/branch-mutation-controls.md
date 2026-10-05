<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Mutation controls in a branch

## Brief

ADR 0007 keeps Kea and plugin rows shared with main. The refusal middleware protects writes, but enabled controls still let a user open a form or attempt a change. Every plugin page must show these controls disabled with a reason while a branch is active. Main must retain its current behavior.

The seam is the rendered HTTP response, including HTMX fragments. Tests cross that seam through real Django requests and a real browser. They use provisioned branches and the existing transport stub only for external Kea commands.

The owning module must derive unsafe targets from the middleware policy and mutation-form targets from `_KeaChangeMixin` and NetBox's generic mutation views. No separate URL list is permitted. A page walk must detect controls that escape the policy.

## Independent coordinator proposal

Process HTML responses centrally in the refusal middleware while a branch is active. Resolve link, form, submit-override, and HTMX targets against the request URL. Use the middleware's callback ownership and method policy for unsafe requests. Recognize mutation-form links by the existing view classes. Use a declared, maintained HTML parser instead of regular expressions or a custom DOM implementation.

Disable native controls and remove active navigation and HTMX mutation attributes from disabled controls. Keep read navigation, search, export by safe methods, and cancellation working. Explain the disabled state with accessible attributes and a tooltip that remains available when a native button is disabled.

Rejected alternative for comparison: template attributes or a template tag at every control. It repeats knowledge across templates, inline table templates, and inherited NetBox controls. A new site can omit the tag. A browser-only transformation also permits enabled controls before JavaScript runs and does not meet the rendered-response acceptance condition.

## Evidence

- `branching.BranchRefusalMiddleware.process_view` refuses unsafe methods on `plugin_owned` callbacks.
- `_KeaChangeMixin` identifies views that change live Kea state.
- Server mutation controls include inherited NetBox generic views.
- `SyncJobsView` has read navigation and unsafe forms on the same URL.
- Table rows contain mutation links and HTMX buttons. HTMX responses can replace them after the initial page load.
- The existing browser refusal tests exercise the middleware backstop. They must continue to verify refusal after intentionally bypassing the disabled UI.

## Design comparison

The independent designer was GPT-6.1 Sol at high reasoning. Its factual packet named the problem, constraints, acceptance conditions, and existing source files. It contained no coordinator proposal. Both proposals were complete before comparison.

| Decision | Coordinator | Independent designer | Disposition |
| --- | --- | --- | --- |
| Owning seam | Middleware, final HTML | Middleware, final HTML | Adopt. It covers inherited controls and HTMX fragments. |
| Parser | Declared maintained parser | BeautifulSoup with `html.parser` | Adopt `beautifulsoup4>=4.12,<5`. NetBox does not supply it. |
| Mutation navigation | Mixin and generic mutation classes | Same, with a separate predicate | Adopt one callback predicate in `branching.py`. |
| Tooltip | Accessible explanation on disabled controls | Focusable wrapper with a native title | Adopt the wrapper. Disabled buttons cannot reliably receive pointer or focus events. |
| Existing browser refusal tests | Intentionally bypass disabled UI | Keep a main page open, select a branch in another tab | Adopt stale enabled pages. This is a reachable user scenario. |

## Candidate r2

Keep branch selection, refusal text, unsafe callback policy, and mutation-form classification in `branching.py`. Share the unsafe callback predicate with middleware. Classify form navigation by `_KeaChangeMixin` and NetBox generic edit, delete, bulk edit, bulk delete, and bulk import classes. Readable mixed-method pages remain navigable.

Add a separate HTML transformation module. It resolves same-origin targets relative to the request URL and examines links, forms, submit overrides, external form-associated controls, and unsafe HTMX verbs. Remove active request attributes from refused controls. Disable native controls. Keep labels and use a focusable wrapper with an explanation visible on hover and keyboard focus, associated through `aria-describedby`. Do not add controls that permissions or unavailable data withheld.

Only active-branch, non-streaming HTML responses from plugin callbacks are transformed. Main and absent-plugin responses bypass parsing. Non-HTML responses are unchanged. Remove stale content-length metadata after transformation. Resolve targets with narrow `Resolver404` handling; parser and programming errors fail visibly.

Validate raw rendered HTML before JavaScript, a route-derived page walk in a provisioned branch, explicit known mutation and read controls, unannotated new callbacks, submit overrides, and HTMX fragments. Run browser coverage on the six requested page families and verify main controls. Keep middleware refusal tests using stale enabled pages selected into a branch through a second tab.

## Ratification

Round 1 returned NEEDS-WORK for r1: a native `title` is not reliably visible on keyboard focus. This blocker is accepted. Candidate r2 replaces the native-title-only approach with a CSS tooltip visible on both hover and focus. The wrapper associates the tooltip through `aria-describedby`. Browser tests must perform both interactions and assert visible explanation text, including after HTMX replacement.

Round 2 returned RATIFY r2 for this scope. Astra reviewed at high reasoning in a read-only sandbox. It found no other blocker. The implementation must prove the rendered page walk, submission semantics, the six browser families, hover and focus tooltips after HTMX updates, response handling, and stale-page refusal.

## Implementation evidence

The Server response and browser regressions failed before the transformation and passed after it. Native submission tests exposed default submit buttons, implicit Enter submission, external form owners and safe GET overrides. A separate regression exposed inherited HTMX requests that blocked nested reads. Each failure was verified before its fix.

The route-derived page walk compares populated main and branch pages, including HTMX fragments. An independent scan rejects remaining mutation targets. The browser coverage checks all six requested page families, hover and keyboard explanations, tooltip clipping, a refreshed lease table and native Enter submission. It compares real Kea and NetBox state before and after the interactions. Stale enabled pages still exercise the refusal backstop.

Main responses, absent-plugin responses, JSON and streaming responses retain their behavior. The response tests also cover the real compression middleware chain and distinct tooltip identifiers across fragments.

## Response encoder ordering, candidate r3

The rendered-response seam assumes decoded HTML. An additional inner gzip middleware can encode the response before the transformer and cause a decode error. NetBox appends plugin middleware in plugin order, then imports local settings. Its plugin validation hook therefore cannot inspect the complete chain. The accepted middleware owner must validate the completed chain without mutating another plugin's configuration.

The coordinator and an independent GPT-6.1 Sol designer considered the same factual brief without seeing each other's proposals. Both selected validation in `branching.py`, called during plugin readiness and middleware construction. The class guard recognizes Django gzip middleware and its subclasses and requires each encoder before every branch-refusal middleware in request order. This makes encoding run after HTML transformation on the response path. An invalid order raises an actionable configuration error before a view runs. Without branching, the guard is inactive.

| Decision | Coordinator | Independent designer | Disposition |
| --- | --- | --- | --- |
| Owner and seam | Completed Django middleware chain in `branching.py` | Same | Adopt. Both readiness and rebuilt handlers use one validator. |
| Configuration | Validate and explain the order | Same | Adopt. Do not silently reorder another plugin. |
| Encoder detection | Django gzip and subclasses | Same | Adopt a bounded class guard. Generic middleware factories cannot be reliably classified as encoders. |
| Unknown encoded HTML | Explain the unsupported ordering before decoding | Same | Adopt a diagnostic backstop. Do not skip mutation disabling. |

Decompressing and recompressing is rejected because it transfers compression and metadata ownership and can discard Django's security padding. Returning encoded HTML without transformation is rejected because it keeps enabled mutation controls. Moving policy into individual views or templates is rejected because it loses coverage of inherited controls and fragments.

Acceptance for r3: a real correctly ordered gzip chain returns compressed disabled controls with valid length and `Vary`; known wrong order fails while loading the chain and at readiness; subclasses and duplicate mixed-order encoders are checked; absent branching retains its behavior. A custom pre-encoded HTML response raises an explicit configuration error without a silent bypass. The remaining response-type guards continue to pass.

Astra at high reasoning ratified r3 in a read-only sandbox. The implementation uses one validator at readiness and handler construction. The real regressions failed before the fix: incorrect middleware order was accepted, and a pre-encoded response raised a decode error. After the fix, the complete branching module passed with 84 tests and 1,107 subtests. The absent-plugin module passed with nine tests. The correctly ordered chain retains compressed disabled controls, response length and `Vary`. Main, non-HTML and streaming response controls also pass.
