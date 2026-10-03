<!--
SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

**Safe to capture as throwaway evidence and publish the specification with explicit limitations. No production-readiness approval.** I reviewed the isolated worktree and evidence read-only. I made no edits, Git mutations or database changes.

The final [guard run](/tmp/kea-mapping-prototype-20261003/safety-green.log:6) records **14 passed, 16 subtests passed**. These totals include inherited lifecycle tests running twice.

1. **CONFIRMED: bounded squash feasibility.** The [lifecycle tests](/home/mzieba/workspace/netbox-kea/.worktrees/prototype-dhcp-mappings/netbox_kea/tests/test_prototype_dhcp_mappings.py:70) exercise real import, native deletion/merge/revert and reimport for both target kinds, both families and instance/queryset deletion. They assert original mapping and target PKs, source identity and zero duplicate creation.

2. **CONFIRMED: the squash restriction is supported, with an evidence limitation.** The reported iterative control failed resolving the absent GFK target. Inspected cached native code corroborates reverse chronological restoration and target resolution before mapping restoration. However, the retained lifecycle log contains only aggregate results, and the current iterative test now checks refusal. Preserve the original control source/output if available; do not describe the current refusal test as independently reproducing that failure.

3. **CONFIRMED: preflight alone is insufficient.** The [safety-red log](/tmp/kea-mapping-prototype-20261003/safety-red.log) records both behavioral failures before guards. A real import occurs after preflight; an old schema permits mapping queries to reach main. Transaction-local validation and schema-aware routing checks are justified.

4. **CONFIRMED only for tested paths.** These concrete gaps remain:
   - **Newer-main revert:** create a Global Reservation in a branch, merge it, let main import adopt it and create a mapping, then revert. Undoing the branch creation deletes the target and its new mapping because both [deletion guards](/home/mzieba/workspace/netbox-kea/.worktrees/prototype-dhcp-mappings/netbox_kea/branching.py:376) check coverage only during `merging`.
   - **Lock ordering:** transactional mapping `update_or_create()` can acquire its row lock before the signal requests the advisory lock. The importer acquires those locks in the opposite order, permitting deadlock. Nonatomic target saves also bypass the advisory lock.
   - **Public reads and exemptions:** [compute_drift()](/home/mzieba/workspace/netbox-kea/.worktrees/prototype-dhcp-mappings/netbox_kea/views/dhcp_plugin_sync.py:169) returns `mapping_unavailable`, but the template ignores it and displays “No DHCP versions enabled.” Exempting mappings after provisioning can also leave a table that passes the schema check while configured routing returns main data.
   - **Timestamp-only imports:** Global Reservation imports still update mappings and `last_synced`. Separation from semantic history is unfinished. Optional-plugin configurations and dry runs remain unproved.

5. **CONFIRMED non-tautology; NEEDS-WORK for whole-operation rollback coverage.** The [two-thread regression](/home/mzieba/workspace/netbox-kea/.worktrees/prototype-dhcp-mappings/netbox_kea/tests/test_prototype_dhcp_mappings.py:182) uses native merge, separate connections, explicit synchronization and the real importer. It asserts refusal plus target and newly imported mapping preservation. It contains no test-only signal-history clearing, so its green result supports reachable provenance through the prototype’s preaction clearing.

   **REFUTED:** that test proves rollback of earlier unrelated changes. It contains no such mutation and checks neither branch status nor applied-history rollback. Also, prototype clearing lacks the existing tracked job’s final cleanup.

The [specification](/tmp/kea-mapping-prototype-20261003/spec-draft.md:72) and [ticket](/tmp/kea-mapping-prototype-20261003/ticket-draft.md:9) correctly present these broader guarantees as future requirements. I found no load-bearing specification conflict. Attach the final evidence and state the rollback and iterative-control limitations when publishing; these gaps do not prevent capturing bounded feasibility.


## Preserved control follow-up

**CONFIRMED: safe to capture as bounded throwaway evidence. No production-readiness approval.**

- **CONFIRMED: non-tautological control.** The [test](/home/mzieba/workspace/netbox-kea/.worktrees/prototype-dhcp-mappings/netbox_kea/tests/test_prototype_dhcp_mappings.py:132) imports a real IPv4 Subnet and mapping, deletes them in a branch, performs native iterative merge, and asserts both main rows are absent. Native revert must then raise the target model’s actual `DoesNotExist`. No exception or replay order is fabricated. Successful dependency restoration would make this test fail.

- **CONFIRMED: dependency-order evidence.** Native iterative undo processes reverse chronology and resolves the mapping’s generic target before saving the restored mapping. This supports the concrete missing-target scenario. The test disconnects the two preaction receivers, leaves save/delete guards connected, and reconnects in `finally`. Precisely, those receivers also perform schema checks and history clearing; the control explicitly restores history clearing. That does not invalidate this fresh-branch probe.

- **CONFIRMED: completed output is now available.** The [log](/tmp/kea-mapping-prototype-20261003/iterative-control.log:17) records:
  ```text
  PROTOTYPE_RESULT iterative restoration: DoesNotExist: Subnet matching query does not exist.
  1 passed in 252.64s (0:04:12)
  ```
  The coordinator’s required pass is present.

The README and updated specification/ticket explicitly retain the newer-main adoption/revert defect and the rollback, locking, routing, history, optional-plugin and dry-run requirements as production work. They do not block evidence-only capture. The baseline regression is preserved as a patch, and `test_branching.py` matches HEAD.

No new load-bearing capture blocker found. No edits, mutations or test execution performed.
