# Task 3 Re-Review: Fix Round 3

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- Prior N1 crash-state stranding: **ADDRESSED**
- New Critical findings: none
- New Important findings: 1

This was a scoped static re-review of Fix Round 3. Per instruction, the recorded test runs were not rerun.

## Prior Finding

### N1. Interrupted ownership transitions strand deterministic paths: ADDRESSED

Ownership receipts now persist the expected baseline and append state history atomically with each status change ([scripts/workspace_manager.py:595](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:595), [scripts/workspace_manager.py:634](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:634), [scripts/workspace_manager.py:652](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:652)). `create()` and `reconcile()` serialize full recovery operations through an advisory file lock, so duplicate same-token restart workers cannot race their Git add/cleanup phases ([scripts/workspace_manager.py:118](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:118), [scripts/workspace_manager.py:265](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:265), [scripts/workspace_manager.py:776](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:776)).

The state branches now converge as follows:

- `RESERVED` with a matching registered checkout becomes `ACTIVE`; absent or owned partial content returns `RETRY_CREATE` after cleanup ([scripts/workspace_manager.py:328](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:328)).
- `FAILED`/`CLEANING` converges to a clean `FAILED` tombstone that the same owner can transition back to `RESERVED` ([scripts/workspace_manager.py:371](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:371), [scripts/workspace_manager.py:170](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:170)).
- `REMOVING` continues or finalizes removal; `REMOVED` returns idempotent absence and can be re-entered by the same token without losing history ([scripts/workspace_manager.py:409](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:409), [scripts/workspace_manager.py:452](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:452)).

Repository/run/task/token checks precede reconciliation and do not echo the durable owner token on mismatch ([scripts/workspace_manager.py:293](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:293)). The new tests cover crashes before add, after add, before Git remove, after Git remove, same-owner `FAILED`/`REMOVED` retry, legacy receipts, foreign tokens, wrong-revision registration, and concurrent same-token re-entry ([scripts/tests/test_workspace_recovery.py:230](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:230), [scripts/tests/test_workspace_recovery.py:314](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:314), [scripts/tests/test_workspace_recovery.py:400](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:400), [scripts/tests/test_workspace_recovery.py:442](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:442), [scripts/tests/test_workspace_recovery.py:516](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:516), [scripts/tests/test_workspace_recovery.py:623](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:623)). The prior permanent-stranding defect is fixed.

## New Important Finding

### N2. Reconciliation treats an unqueryable or replaced registration as owned cleanup state

`_registered_worktrees()` maps every `git worktree list` failure to an empty set because `_git()` returns `None` and the helper substitutes `""` ([scripts/workspace_manager.py:696](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:696), [scripts/workspace_manager.py:755](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:755)). Recovery cannot distinguish “confirmed unregistered” from “registration query failed.” That ambiguity is destructive:

- A `RESERVED` receipt with an existing path calls `shutil.rmtree()` when `registered` is false, so a transient Git query failure can recursively delete a valid registered checkout ([scripts/workspace_manager.py:328](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:328), [scripts/workspace_manager.py:352](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:352)).
- `FAILED`/`CLEANING` likewise force-removes any reported registration without checking its `HEAD` against `baseline_revision`, then recursively deletes the path ([scripts/workspace_manager.py:371](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:371)). A foreign replacement registered at the deterministic path after the crash is therefore removed by the matching historical token even when its revision does not match the receipt.
- `REMOVING` performs the same unconditional force removal for a reported registration and does not verify that the checkout still matches the authorized baseline before destructive continuation ([scripts/workspace_manager.py:409](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:409)).

The `RESERVED -> ACTIVE` branch correctly fails closed on a known wrong revision, but the destructive branches do not apply that rule. Registration discovery must return a tri-state result (`registered`, `not registered`, `query failed`) and every query failure must stop recovery. Before force removal, verify the registered checkout's canonical path and `HEAD` against the receipt; on mismatch, leave both path and registration untouched. Also recheck source `HEAD` before completing a recovered `RESERVED -> ACTIVE`, matching normal creation's changed-baseline rule ([scripts/workspace_manager.py:97](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:97), [scripts/workspace_manager.py:201](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:201)).

The current mismatch regression covers only `RESERVED` with a successfully queried registration ([scripts/tests/test_workspace_recovery.py:356](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:356)). Add query-failure cases and wrong-revision registered paths for `FAILED`, `CLEANING`, and `REMOVING`, asserting no file, uncommitted change, or registration is removed.

## Scope Notes

- Foreign-token rejection, baseline persistence/migration, normal registration validation, crash-state convergence, removed-tombstone idempotency, and duplicate same-token recovery serialization are otherwise sound in the reviewed scope.
- No other new Critical or Important issue was found. The failing verdict is solely due to fail-open registration/baseline handling in destructive reconciliation branches.
