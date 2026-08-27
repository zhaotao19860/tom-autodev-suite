# Task 3: Durable State, Artifacts, Worktrees and Recovery

## Files

- Modify `scripts/state_store.py`, `artifact_store.py`, `approval_ledger.py`, and `workspace_manager.py`.
- Create `scripts/lock_manager.py` and `scripts/recovery.py`.
- Add/update `scripts/tests/test_state_and_artifacts.py`, `test_gates.py`, and `test_workspace_recovery.py`.

## Interfaces

- `StateStore.intent(run_id, operation, idempotency_key, payload) -> dict`
- `StateStore.receipt(intent_id, response, evidence_refs) -> dict`
- `ArtifactStore.get(artifact_id) -> dict | None`
- `LockManager.acquire(key, owner_token, ttl_seconds) -> dict`
- `WorkspaceManager.create(repo_path, run_id, task_id, baseline) -> dict`
- `Recovery.resume(run_id) -> dict`

## Required behavior

1. Add SQLite persistence for external intents/receipts, artifact index, locks, heartbeats, handoffs, and external results without breaking append-only events or existing idempotency records.
2. An external intent is persisted before any side effect. If a process stops after the intent, recovery returns `QUERY_REQUIRED` for that operation and must not repeat it. Receipts are idempotent and immutable for the same intent; conflicting duplicate responses fail closed.
3. Artifact lookup returns content and metadata only after verifying stored hashes. Missing or corrupted content returns a stable reason and never masquerades as valid evidence.
4. Create a real isolated Git worktree from the exact recorded baseline using argument arrays. Never return the source checkout as `worktree_path`, never stash/move/overwrite user changes, and reject an unverified or changed baseline. Tests use temporary repositories only.
5. Lock acquisition is atomic. Record key, owner token, PID, TTL, created time, and heartbeat. Reject an active owner. Stale takeover is allowed only when heartbeat/TTL are expired and the recorded owner process is not alive; make takeover explicit in the receipt.
6. Recovery resumes from the last committed checkpoint and reports every uncertain external intent as a query action before any retry. It must expose active/stale locks and incomplete handoffs deterministically.
7. Approval ledger additions must preserve Comate+Infoflow-only authorization, input-hash binding, first-valid decision, and later-response audit.
8. All returned reason codes and list ordering are stable; no secrets are placed in persistence payloads by helper code.
9. Do not initialize Git in the Skill directory or run BGW/XFlow builds/tests. Do not create a production worktree during tests.

## Test sequence

Write failing tests first and record RED. Implement the minimum, run focused tests, then run `python3 -m unittest discover -s scripts/tests -v` from the Skill root.

## Environment note

`/Users/tom/Desktop/skills` is not a Git repository, so no commits or plan worktree can be created. Production worktree behavior must be verified entirely with temporary Git repositories.
