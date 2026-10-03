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
