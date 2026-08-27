# Task 3 Re-Review: Fix Round 2

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- C2: **ADDRESSED**
- I2: **ADDRESSED**
- Remaining prior Critical/Important findings: none
- New Critical findings: none
- New Important findings: 1

This was a scoped static re-review of the two findings remaining after Fix Round 1. Per instruction, the recorded test runs were not rerun.

## Prior Findings

### C2. Evidence-reference secret persistence: ADDRESSED

`StateStore.receipt()` now calls `validate_evidence_refs()` before either `json.dumps` or opening the write transaction ([scripts/state_store.py:223](/Users/tom/Desktop/skills/tom-autodev/scripts/state_store.py:223), [scripts/state_store.py:229](/Users/tom/Desktop/skills/tom-autodev/scripts/state_store.py:229)). The validator requires a real list of unique strings, rejects secret-bearing text with a constant error, and accepts only a full-match canonical grammar: a local ID, `artifact:<id>`, `ku:<document>/<version>`, or `ipipe:<build>/<stage>` ([scripts/persistence_policy.py:11](/Users/tom/Desktop/skills/tom-autodev/scripts/persistence_policy.py:11), [scripts/persistence_policy.py:24](/Users/tom/Desktop/skills/tom-autodev/scripts/persistence_policy.py:24)). URL/query/fragment forms cannot reach either `receipts` or `external_results`.

The regression covers wrong container types, URLs, query strings, fragments, malformed KU/iPipe forms, duplicates through validator behavior, secret-bearing references, and all supported canonical forms; it also scans the SQLite files for the sentinel ([scripts/tests/test_state_and_artifacts.py:280](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_state_and_artifacts.py:280)). The previously open secret-persistence path is closed.

### I2. Foreign/concurrent worktree cleanup: ADDRESSED

Creation reserves the exact canonical worktree path in SQLite before Git runs and binds it to repository, run, task, and a random owner token under `BEGIN IMMEDIATE` ([scripts/workspace_manager.py:114](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:114), [scripts/workspace_manager.py:241](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:241)). A competing creator sees `WORKTREE_ALREADY_RESERVED` and never enters cleanup. Partial cleanup first performs a token/repository/path/status-bound `RESERVED -> CLEANING` transition; without that ownership transition it does nothing ([scripts/workspace_manager.py:222](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:222), [scripts/workspace_manager.py:287](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:287)).

Public removal requires the matching durable record and owner token, serializes `ACTIVE -> REMOVING`, preserves a `REMOVED` tombstone for successful repeat calls, and rejects foreign registered worktrees before invoking force removal ([scripts/workspace_manager.py:154](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:154), [scripts/workspace_manager.py:172](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:172), [scripts/workspace_manager.py:177](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:177), [scripts/workspace_manager.py:183](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:183)). Regressions verify cross-instance token use, missing/wrong tokens, repeated successful removal, simultaneous same-path creation, and preservation of an uncommitted foreign worktree ([scripts/tests/test_workspace_recovery.py:144](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:144), [scripts/tests/test_workspace_recovery.py:178](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:178), [scripts/tests/test_workspace_recovery.py:209](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_workspace_recovery.py:209)). The foreign/concurrent deletion issue from I2 is fixed.

## New Important Finding

### N1. Interrupted ownership transitions permanently strand deterministic worktree paths

The durable ownership state machine has no resume or reconciliation path. `_reserve()` rejects every existing row regardless of whether its state is `RESERVED`, `FAILED`, `REMOVING`, or `REMOVED` ([scripts/workspace_manager.py:250](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:250), [scripts/workspace_manager.py:256](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:256)). `remove()` only begins from `ACTIVE`; any other non-`REMOVED` state returns `WORKTREE_NOT_ACTIVE` ([scripts/workspace_manager.py:177](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:177), [scripts/workspace_manager.py:183](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:183)).

Consequently:

- A crash or filesystem exception after reservation but before `ACTIVE` leaves `RESERVED` forever.
- A transient `git worktree add` failure records `FAILED`, and the same run/task can never retry its deterministic path ([scripts/workspace_manager.py:120](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:120), [scripts/workspace_manager.py:237](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:237)).
- A crash after `ACTIVE -> REMOVING`, including after Git successfully removes the checkout but before the `REMOVED` update, makes every later remove return `WORKTREE_NOT_ACTIVE` ([scripts/workspace_manager.py:183](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:183), [scripts/workspace_manager.py:206](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:206), [scripts/workspace_manager.py:213](/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:213)).

This is a durable recovery regression: the ownership receipt prevents unsafe cleanup, but cannot recover its own uncertain external Git operation. Add reconciliation keyed by the matching owner token that queries Git registration, path existence, and expected revision, then deterministically completes or rolls back `RESERVED`/`CLEANING`/`REMOVING`; permit a controlled new reservation after a terminal `FAILED` or `REMOVED` tombstone without losing audit history. Add interruption tests at each persistence/Git boundary. The current tests cover completed calls and simultaneous creators, not restart from intermediate durable states.

## Scope Notes

- No other new Critical or Important issue was found in the scoped Fix Round 2 changes.
- The six original findings C1/C2/I1-I4 are now addressed. The failing verdict is solely due to the new crash-recovery gap in the ownership lifecycle.
