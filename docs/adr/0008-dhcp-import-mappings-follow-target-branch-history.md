---
status: proposed
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
object and must remain recoverable with it. This is a specific proposed exception to ADR 0007: Server,
settings, imports, IPAM ownership and live Kea operations retain their current main-only policy.

We choose tracked target and mapping history over a separate persistent deletion receipt. The trade-off is
new replay, concurrency and schema-compatibility work. Affected operations require fresh branches. Merge
and revert must preserve newer main state through atomic refusal when history or identities conflict.

The policy and acceptance conditions are in [the design record](../design/dhcp-import-mapping-branching.md).
The operator accepted its nine policy choices and confirmed the complete agreement. The isolated real-DB
prototype demonstrated recovery with dependency-aware squash for both supported targets and families.
Installed iterative replay failed to restore the mapping dependency, so affected deletions require squash.
Two unsafe controls, late main import and old-schema deletion, failed before prototype guards and passed
with those guards. The design record states the remaining production validation gates. This ADR remains
proposed until the complete exception is implemented and accepted.
