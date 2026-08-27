# Task 8 Fix Round 1 Scoped Re-review

## Verdicts

- **Specification: FAIL.** Five original findings are addressed, but `C1`, `C2`, `I5`, and `I6` remain open. Mandatory archival, failure-atomic recovery, symlink/TOCTOU confinement, and secret redaction are still incomplete.
- **Standards/security: FAIL.** Cross-process ownership is unsafe, rollback journals are unauthenticated and unconfined, and two new idempotency/input-validation regressions exist.
- **Overall: BLOCKED / FIX ROUND 2 REQUIRED.** Do not mark Task 8 complete or advance its output into Task 9/10.

## Verification

- Reviewed the round-1 package, Task 8 brief, initial review, round-1 implementation report, and the exact three fix files.
- The three round-1 SHA-256 values in `task-8-report.md` match the current files.
- `python3 -m unittest scripts/tests/test_run_summary.py -v`: **18/18 PASS**.
- `python3 -m unittest discover -s scripts/tests -v`: **394/394 PASS**.
- `python3 -m compileall -q scripts`: **PASS**.
- Reproductions used only temporary SQLite/artifact roots, fake KnowledgeSync/validation dependencies, temporary files, and mocks. No live external write or BGW/XFlow project command ran.

## Original Finding Matrix

| ID | Verdict | Evidence |
|---|---|---|
| C1 | **NOT ADDRESSED** | Missing/ordinary raising KnowledgeSync is now blocked, but the durable `PROPOSED` row is still created before proposal archival (`scripts/run_summary.py:63-69`). A process interruption in that gap leaves an indistinguishable applyable `PROPOSED`. Rollback and crash-recovery result archival is still ignored before terminalization (`scripts/run_summary.py:106-112`, `scripts/run_summary.py:139-146`). |
| C2 | **NOT ADDRESSED** | A durable journal/CAS was added, but any different PID is treated as a dead owner and rolled back without liveness, heartbeat, owner-token, or approval verification (`scripts/run_summary.py:78`, `scripts/run_summary.py:139-146`; `scripts/state_store.py:550-585`). Rollback failure still finalizes `ROLLED_BACK` (`scripts/run_summary.py:106-112`). |
| C3 | **ADDRESSED** | The immutable proposal envelope now binds envelope hash, proposal ID, run ID, candidate hash, and denormalized columns on every load (`scripts/run_summary.py:124-130`; `scripts/state_store.py:521-548`). APPLIED/ROLLED_BACK terminal replay checks the persisted approval identity and live ledger (`scripts/run_summary.py:132-137`). The separate REJECTED replay regression is `N2` below. |
| I1 | **ADDRESSED** | `RELEASE_SUCCESS` is the only success state and it takes precedence over historical repaired failures (`scripts/run_summary.py:34-49`). The new test covers repaired success. |
| I2 | **ADDRESSED** | Summary/G10 artifact kinds are excluded from source metrics (`scripts/run_summary.py:24`, `scripts/run_summary.py:38-49`); repeated unchanged builds reuse the same hash/artifact in the focused test. |
| I3 | **ADDRESSED** | Failure grouping and candidate selection now consume nested production `payload.evidence`, require candidate schema version `1`, and recognize fail/timeout/error families (`scripts/run_summary.py:148-168`). Malformed candidate handling is a separate new regression, `N3`. |
| I4 | **ADDRESSED** | `propose()` loads the supplied artifact from ArtifactStore, verifies integrity/kind/run/content hash, and then uses the loaded bytes rather than caller evidence (`scripts/run_summary.py:51-61`, `scripts/run_summary.py:114-122`). |
| I5 | **NOT ADDRESSED** | Initial symlink roots/ancestors are rejected, but confinement remains check-then-use. `_revalidate_target()` returns before `_atomic_write()` opens the parent (`scripts/run_summary.py:207-211`, `scripts/run_summary.py:244-250`), and rollback performs no root/symlink validation (`scripts/run_summary.py:213-220`). A parent-directory swap in that window wrote outside `control_root` and returned `OK`. |
| I6 | **NOT ADDRESSED** | The expanded regex still misses quoted JSON/Python keys. Redacting `{"Authorization": "Bearer actual-secret", "api_key": "second-secret"}` leaked `second-secret`, and `_has_secret('{"api_key": "second-secret"}')` returned false (`scripts/run_summary.py:19-21`, `scripts/run_summary.py:267-269`). No new boundary-redaction test was added. |

## Open Finding Details

### C1 - Proposal/result archival still has crash and terminal-loss gaps

`save_optimization_proposal()` writes status `PROPOSED` before `_archive()` (`scripts/run_summary.py:63-69`). `PROPOSED` therefore means both “verified KU archive completed” and “process died before archive started/completed.” Simulating cancellation with `KeyboardInterrupt` during `publish_phase()` left `PROPOSED`; a later controller accepted a G10 approval and applied the candidate. Ordinary `Exception` conversion does not close a process-loss window. Persist `ARCHIVING`, then transition to an applyable `PROPOSED`/`ARCHIVED` only after verifying a durable KnowledgeSync receipt; `apply()` must recheck that receipt.

After validation/apply failure, the second result `_archive()` return is discarded and status is finalized (`scripts/run_summary.py:106-112`). Crash recovery does not attempt result archival at all (`scripts/run_summary.py:139-146`). A terminal local row is therefore allowed without the mandatory KU result. Persist an explicit archive-pending handoff/result and make recovery finish or verify the KnowledgeSync receipt idempotently before final status.

### C2 - Cross-process concurrency and rollback-failure semantics are unsafe

`claim_optimization_apply()` records PID/token/journal, but `_recover_or_wait()` considers an owner active only when its PID equals the caller's PID (`scripts/run_summary.py:139-146`). A second live process necessarily has a different PID, so it can roll back the first process while that process is still writing or validating; it does not need the stored approval. A reproduction with an active claimed owner and mocked second PID returned `G10_INTERRUPTED_ROLLED_BACK`, restored the file, and terminalized the row. Use a durable lease/heartbeat plus OS liveness and owner token; a non-owner may recover only after proven expiry/death.

When `_rollback_journal()` returns false, an `_ApplyFailure` still keeps its misleading `...ROLLED_BACK` reason, and all failures are finalized with status `ROLLED_BACK` (`scripts/run_summary.py:106-112`). The reproduction returned `G10_VALIDATION_FAILED_ROLLED_BACK` while the file remained at candidate bytes and the row was `ROLLED_BACK`. Rollback failure must remain nonterminal/recoverable (`RECOVERY_REQUIRED`/`ROLLBACK_FAILED`) with its journal intact; it must never claim rollback success.

### I5 - Path checks do not make filesystem writes race-safe

The initial root/ancestor checks close the prior simple symlink case (`scripts/run_summary.py:170-179`, `scripts/run_summary.py:181-197`), but path revalidation and write are separate operations. Swapping `scripts/` for a symlink after `_revalidate_target()` and before `_atomic_write()` caused `apply()` to return `OK`, wrote `after` outside `control_root`, and left the original in-root file unchanged. Use directory file descriptors with no-follow semantics and inode/root identity checks for journal, write, replace, and rollback operations; do not trust a re-resolved string path across the write boundary.

### I6 - Quoted secret keys bypass both rejection and publication redaction

The regex expects the delimiter immediately after `authorization`, `api_key`, etc. Quotes between a key and colon bypass API-key detection and can partially consume Authorization values while leaving malformed/leaking output. Use the established structured redaction where input is structured, and a tested string redactor that covers quoted keys, headers, URLs, boundary splits, and common separators. Candidate content must be rejected before proposal persistence whenever a canonical secret detector finds material.

## New Critical/Important Findings

### N1 Critical - The new rollback journal is unauthenticated and can write outside the control root

`apply_journal_json`, approval ID, owner PID, and owner token are loaded without a journal hash or proposal/candidate/root binding (`scripts/state_store.py:663-678`). `_rollback_journal()` trusts each persisted path and base64 payload, ignores the stored `sha256`, and calls `_atomic_write()` without allowed-root or symlink checks (`scripts/run_summary.py:213-220`). Tampering only `apply_journal_json`, then invoking different-PID recovery, overwrote a temporary victim outside `control_root` with attacker bytes and returned `G10_INTERRUPTED_ROLLED_BACK`. Give the journal its own canonical hash bound to proposal/candidate/approval/roots/owner, verify every entry and before hash on load, and perform confined no-follow rollback writes.

### N2 Important - A legitimate REJECTED replay cannot authenticate

The reject path stores `approval_id` only inside `result`, using `update_optimization_proposal()` rather than the approval column (`scripts/run_summary.py:82-88`; `scripts/state_store.py:597-620`). `_terminal_replay()` requires `stored.approval_id == approval_id` (`scripts/run_summary.py:132-137`), so the first call returns `G10_REJECTED` but an identical second call returns `G10_REPLAY_AUTH_REQUIRED`; the column is `NULL`. Persist the rejecting approval identity atomically with terminal status and add same/wrong-approval replay tests for every terminal state.

### N3 Important - Malformed versioned production candidates raise instead of failing closed

`_normalize_candidate()` checks required values for truthiness but not the four narrative fields for string type (`scripts/run_summary.py:181-197`). A version-1 candidate with `root_cause=123` reaches `_redact()` and raises `TypeError` rather than returning a stable rejection. Restore explicit field/type/schema validation before redaction/hash/path work and test every malformed production-envelope field.

## Test Assessment

The four added tests close the simple success/repeat-build, missing-sync/forged-summary, nested-envelope, and symlink-root cases. The report claims validation of crash, concurrency, archive exception, run tamper, terminal replay, ancestor/TOCTOU, and boundary secrets, but `test_run_summary.py` contains no new tests for those paths; the focused suite still passes while all reproductions above fail. Round 2 needs durable regression tests for every open/new finding.

M1 mode preservation was implemented in `_atomic_write()`. M2 remains documentation-only; the round-1 report says 390 full tests, while the current exact suite runs 394.
