---
status: accepted
date: 2026-10-03
---

<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# DHCP Import Mappings follow their targets' branch history

Deleting an imported DHCP object through a branch can remove its main-only mapping, while revert restores
the object without that mapping. We select branch-local DHCP Import Mappings that follow Subnets and Global
Reservations through deletion, merge and revert, because the association describes the imported NetBox
object and must remain recoverable with it. This is a specific exception to ADR 0007: Server,
settings, imports, IPAM ownership and live Kea operations retain their current main-only policy.

We choose tracked target and mapping history over a separate persistent deletion receipt. The trade-off is
new replay, concurrency and schema-compatibility work. Affected operations require fresh branches. Merge
and revert must preserve newer main state through atomic refusal when history or identities conflict.

The policy and operator recovery rules are in
[the design record](../design/dhcp-import-mapping-branching.md).
The writer boundaries and lock order are in
[the transaction design](../design/dhcp-import-mapping-transactions.md).
Affected actions require dependency-aware squash. Iterative replay cannot restore the target before
its mapping, so the action is refused before mutation. Native history remains the recovery authority;
branch status alone does not authorize replay. Existing schemas and already-lost mappings require
explicit operator action instead of automatic reconstruction.

Tag changes and mapped DHCP target replay must occur in separate branches. Recovery refuses mixed
actions and unavailable or conflicting existing Tags. Operators reconcile Tags before creating a
fresh DHCP branch. This bounded policy avoids interpreting historical Tag names or rebuilding Tags.
