# Task 3 Implementation Report

## Status

Complete. Durable intents, receipts, artifact indexing, lock/heartbeat ownership,
handoffs, external results, isolated Git worktrees, and checkpoint recovery are
implemented. Existing append-only events, idempotency results, and Task 2 approval
authorization remain supported.

## Changed Files

- `scripts/state_store.py`
  - Added SQLite tables for external intents, immutable receipts, external results,
    artifact indexes, locks, heartbeats, and handoffs.
  - Added atomic intent/receipt operations, pending-intent lookup, and handoff
    recording/completion.
- `scripts/artifact_store.py`
  - Added a durable SQLite artifact index and hash-verified lookup for content and
    metadata.
  - Missing or corrupted files return stable reason codes without returning evidence
    content or metadata.
- `scripts/approval_ledger.py`
  - Added durable response audit records and serialized first-valid resolution.
  - Preserved Comate+Infoflow-only authorization and input-hash binding; invalid
    decisions fail closed.
- `scripts/workspace_manager.py`
  - Added detached worktree creation from the exact full recorded baseline using Git
    argument arrays.
  - Baseline mismatch/unverified state is rejected before creating directories, and
    source checkout changes are only recorded, never moved, stashed, or overwritten.
- `scripts/lock_manager.py`
  - Added atomic SQLite lock acquisition, heartbeat history, PID liveness checks,
    active-owner rejection, and explicit stale takeover receipts.
- `scripts/recovery.py`
  - Added deterministic resume output from the latest committed checkpoint, including
    query actions for all uncertain intents, active/stale locks, and incomplete
    handoffs.
- `scripts/tests/test_state_and_artifacts.py`
  - Added durable intent/receipt and artifact corruption tests.
- `scripts/tests/test_gates.py`
  - Added approval late-response audit and invalid-decision tests.
- `scripts/tests/test_workspace_recovery.py`
  - Added temporary-repository worktree tests, atomic lock/heartbeat/stale takeover
    tests, and deterministic recovery tests.

## TDD Evidence

Initial RED command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery -v
```

Result: exit 1. Existing tests passed; six expected errors identified the missing
`StateStore.intent`, `StateStore.receipt`, `ArtifactStore.get`,
`ApprovalLedger.responses`, `lock_manager`, and recovery/workspace interfaces.

Approval validity RED command:

```text
python3 -m unittest scripts.tests.test_gates.ApprovalLedgerTests.test_invalid_decision_cannot_become_effective_or_enter_the_audit -v
```

Result: exit 1. `MAYBE` incorrectly became effective before the decision allowlist was
added.

Focused GREEN command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery -v
```

Result: 31 tests passed.

Full control-plane command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result before the report-only edit: 86 tests passed in 3.351 seconds. The fresh
post-report verification also passed all 86 tests in 3.312 seconds.

## Safety and Concerns

- Every Git repository used by Task 3 tests is created under `TemporaryDirectory`.
- No Git repository was initialized in `/Users/tom/Desktop/skills` or the Skill root.
- No BGW/XFlow build, test, or workspace command was run.
- The workspace is not a Git repository, so no commit was created and repository-based
  diff/status reporting is unavailable.
- No live external side effect or production worktree was created.

## Fix Round 1

All findings from `task-3-review.md` were addressed.

### Changes

- C1: Artifact roots are resolved once to absolute paths. Logical components reject
  absolute/traversal forms, symlink escapes are blocked, and indexed content/metadata
  paths are revalidated under the store root before any read.
- C2: `persistence_policy.py` recursively rejects secret-bearing keys before every
  `StateStore` JSON persistence boundary and before artifact metadata serialization.
  Rejections use a constant reason and do not echo keys' values.
- I1: Artifact retries serialize through SQLite, compare the full existing index, and
  revalidate both stored files. Corrupt files or conflicting index rows raise
  `ARTIFACT_CONFLICT`.
- I2: Worktree roots at or below the source checkout are rejected. Run/task components
  are validated and repository identity is encoded. Source `HEAD` and worktree `HEAD`
  are rechecked after add; failed adds and changed baselines remove partial worktrees,
  prune Git metadata, and remove owned empty directories. `WorkspaceManager.remove()`
  provides containment-checked, idempotent cleanup.
- I3: Approval request lookup/creation now uses `BEGIN IMMEDIATE`, so concurrent
  Comate/Infoflow publishers receive one shared approval ID.
- I4: `Orchestrator.resume()` delegates to `Recovery`; `advance()` and failure routing
  return `RECOVERY_REQUIRED` while uncertain intents exist. Explicit stop and handoff
  reporting remain available.

### RED

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery scripts.tests.test_orchestrator -v
```

Result: exit 1 after 62 tests. The new regressions reproduced artifact path escape,
relative-root failure, secret persistence, corrupt re-put, approval request uniqueness
failure, worktree containment/cleanup/TOCTOU gaps, missing removal, and orchestrator
recovery bypass. Existing unaffected paths remained green.

### GREEN

Focused command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery scripts.tests.test_orchestrator -v
```

Result: 62 tests passed in 3.523 seconds.

Full command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result before this report-only edit: 98 tests passed in 4.590 seconds.

### Fix-Round Safety

- Filesystem and Git attack/cleanup tests use only `TemporaryDirectory` roots and
  temporary Git repositories.
- The post-add race is produced by a temporary Git shim that delegates to the local
  Git binary and changes only the temporary source repository.
- No production repository/worktree, BGW/XFlow command, or live external service was
  used.

## Fix Round 2

The two findings remaining in `task-3-rereview.md` were addressed.

### Changes

- C2: External receipt evidence references are validated before any JSON encoding.
  Only canonical local IDs and the explicit `artifact:`, `ku:document/version`, and
  `ipipe:build/stage` forms are accepted. Non-list, malformed, duplicate, URL-like,
  and secret-bearing values fail with constant reason codes and are never echoed.
- I2: Worktree creation now reserves the exact canonical path in a durable SQLite
  ownership receipt before invoking Git. The receipt binds repository, run, task,
  path, and a generated owner token; lifecycle transitions serialize create,
  cleanup, and removal. Cleanup and public removal verify the token against the
  durable receipt, removed paths retain tombstones, and unowned registered
  worktrees are never force-removed.

### RED

Evidence-reference command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts.StateAndArtifactTests.test_evidence_references_accept_only_canonical_non_secret_ids_before_persistence -v
```

Result: exit 1. All invalid reference cases were accepted, including URL/query forms
and secret-bearing values; the secret sentinel reached SQLite.

Workspace command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_workspace_recovery.WorkspaceCreationTests -v
```

Result: exit 1. Successful creation omitted `owner_token`, removal did not accept an
owner token, foreign paths were removable by containment alone, and concurrent
same-path creation left no surviving `CREATED` worktree because loser cleanup could
delete the winner.

### GREEN

Targeted evidence-reference command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts.StateAndArtifactTests.test_evidence_references_accept_only_canonical_non_secret_ids_before_persistence -v
```

Result: 1 test passed.

Targeted workspace command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests -v
```

Result: 8 tests passed, including durable cross-instance ownership, concurrent
reservation, token mismatch, tombstone, and foreign-worktree preservation cases.

Focused command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery scripts.tests.test_orchestrator -v
```

Result before this report-only edit: 65 tests passed in 4.153 seconds.

Full command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result before this report-only edit: 101 tests passed in 5.404 seconds.

Fresh post-report verification passed all 65 focused tests in 4.288 seconds and all
101 full-suite tests in 5.998 seconds.

### Fix-Round-2 Safety

- All Git activity was performed by tests in `TemporaryDirectory` repositories.
- The ownership database is adjacent to the configured worktree root, so it survives
  checkout removal without placing control metadata inside a worktree.
- Approval code was unchanged; the focused suite reverified Comate+Infoflow request,
  authorization, decision, and audit behavior.
- No Git repository was initialized under `/Users/tom/Desktop/skills`; no production
  worktree/project command, BGW/XFlow build or test, or live external side effect was
  run.

## Fix Round 3

The crash-reconciliation finding in `task-3-rereview-round2.md` was addressed.

### Changes

- Ownership receipts now persist the expected baseline revision and append every
  lifecycle transition to `worktree_ownership_history`.
- `WorkspaceManager.reconcile(repo_path, run_id, task_id, owner_token)` provides a
  deterministic restart result containing the durable owner token, ownership status,
  expected revision, worktree path, and next action.
- Creation and reconciliation use an advisory ownership lock adjacent to the SQLite
  database. It serializes duplicate same-token restart workers across processes and
  is released automatically if a process exits, without adding a durable lease that
  could itself become stranded.
- A matching owner can safely re-enter `create()` with `owner_token`. `RESERVED`
  receipts continue from verified absence, remove only their unregistered partial
  path, or reconcile an existing registration to `ACTIVE` only when its `HEAD`
  matches the recorded baseline. Mismatched registrations fail closed and remain
  untouched.
- `FAILED` and `REMOVED` receipts can be retried by the same owner only after
  reconciliation verifies absence or completes ownership-bound cleanup. Their prior
  state transitions remain in durable history.
- `REMOVING` receipts continue the already-authorized removal for the same token. If
  Git registration and path are already absent, reconciliation finalizes `REMOVED`
  and returns idempotent absence.
- Schema migration adds the baseline column without invalidating round-2 receipts.
  Legacy active receipts remain removable; a legacy reserved receipt can backfill a
  revision only through same-token `create()` after the normal source-baseline checks.

### RED

Crash-recovery command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests -v
```

Result: exit 1. Of 15 tests, the existing eight passed; six new restart cases errored
because `reconcile()` and token-based `create()` re-entry did not exist, and the
registered `REMOVING` restart failed because removal only accepted `ACTIVE`.

Removed-tombstone command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_removed_tombstone_retries_same_owner_without_losing_history -v
```

Result: exit 1. Same-owner recreation returned `BLOCKED` instead of `CREATED`.

Legacy-receipt command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_legacy_active_receipt_without_revision_can_still_be_removed scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_legacy_reserved_receipt_backfills_validated_revision_on_reentry -v
```

Result: exit 1. Both legacy paths returned `BLOCKED`, demonstrating that adding the
new revision field without reconciliation would strand round-2 receipts.

Same-owner concurrent re-entry command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_concurrent_same_owner_reentry_serializes_add_and_preserves_winner -v
```

Result: exit 1. Both duplicate recovery workers returned `BLOCKED`; the losing Git
add claimed `RESERVED -> CLEANING` and removed the winner before it could persist
`ACTIVE`.

### GREEN

Workspace/recovery command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery -v
```

Result before this report-only edit: 23 tests passed in 4.580 seconds.

Focused command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery scripts.tests.test_orchestrator -v
```

Result before this report-only edit: 75 tests passed in 6.474 seconds.

Full command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result before this report-only edit: 111 tests passed in 7.691 seconds.

After adding same-owner recovery serialization, the workspace/recovery command passed
24 tests in 5.966 seconds. Fresh final focused/full counts are recorded in the task
handoff.

### Fix-Round-3 Safety

- Crash points are injected with `SystemExit` immediately after durable reservation,
  after Git add but before `ACTIVE`, after `REMOVING` but before Git remove, and after
  Git remove but before `REMOVED`. Recovery is exercised through a new manager
  instance.
- Foreign-token attempts are checked before cleanup or reconciliation and never
  expose the durable owner token. A registered checkout at the wrong revision retains
  its uncommitted marker and Git registration.
- Every Git operation ran against a repository under `TemporaryDirectory`. No
  production worktree/project command, BGW/XFlow build or test, or live external
  service was used.
- Approval code was unchanged; focused verification retained the Comate+Infoflow
  authorization and audit behavior.

## Fix Round 4

The remaining Important N2 finding in `task-3-rereview-round3.md` was addressed.

### Changes

- Registration discovery now preserves the difference between a successful empty
  `git worktree list --porcelain` result and a query failure. Every reconciliation
  state returns `BLOCKED` / `WORKTREE_REGISTRATION_QUERY_FAILED` /
  `RETRY_RECONCILE` before filesystem or Git removal on query failure.
- Registered worktrees are verified immediately before destructive cleanup. Their
  canonical top level and Git common directory must identify the recorded source
  repository, and `HEAD` must equal the ownership receipt baseline. Repository,
  revision, or missing-baseline mismatches leave the checkout and registration
  untouched.
- An unregistered path is recursively deleted only after Git proves that it is not a
  checkout. A valid or unqueryable checkout returns a stable blocked result without
  modification.
- `RESERVED -> ACTIVE` recovery now rechecks the current source `HEAD` against the
  receipt baseline. Public removal also validates registration, repository identity,
  baseline, and `HEAD` before changing `ACTIVE` ownership state.
- Owner-token serialization is unchanged for authenticated reconciliation results;
  token mismatches still fail before serializing the durable token.

### RED

Required regression command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_registration_query_failure_blocks_recovery_without_removing_the_checkout scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_cleanup_states_leave_registered_wrong_revision_untouched scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_unregistered_foreign_checkout_at_reserved_path_is_never_deleted scripts.tests.test_workspace_recovery.WorkspaceCreationTests.test_reserved_recovery_rechecks_source_head_before_becoming_active -v
```

Result: exit 1. Four test methods produced six expected failures: registration query
failure returned `RECOVERY_REQUIRED`, `FAILED`/`CLEANING` cleanup continued,
`REMOVING` returned `REMOVED`, a foreign checkout was recursively deleted, and a
recovered registered worktree became `ACTIVE` after source `HEAD` moved.

The same-revision foreign-repository regression was mutation-checked by temporarily
removing the Git common-directory comparison. It failed with
`WORKTREE_REMOVE_FAILED` instead of `WORKTREE_REPOSITORY_MISMATCH`; restoring the
identity check returned the test to green.

### GREEN

Workspace/recovery command:

```text
python3 -m unittest scripts.tests.test_workspace_recovery -v
```

Result before the report-only edit: 28 tests passed in 7.012 seconds. The added
same-revision repository-identity test subsequently passed independently.

Focused command:

```text
python3 -m unittest scripts.tests.test_state_and_artifacts scripts.tests.test_gates scripts.tests.test_workspace_recovery scripts.tests.test_orchestrator -v
```

Result before the report-only edit: 81 tests passed in 9.424 seconds.

Full command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result before the report-only edit: 117 tests passed in 10.329 seconds. Fresh
post-report focused/full results are recorded in the task handoff.

### Fix-Round-4 Safety

- All repositories, clones, worktrees, crash states, and foreign replacements used
  by tests were created under `TemporaryDirectory`.
- Query-error and mismatch tests preserve uncommitted markers and verify that Git
  registrations remain present. The unregistered-checkout test verifies that an
  independent repository at the deterministic path survives reconciliation.
- No Git repository was initialized in `/Users/tom/Desktop/skills` or the Skill root.
  No production worktree/project command, BGW/XFlow build or test, or live external
  side effect was run.
