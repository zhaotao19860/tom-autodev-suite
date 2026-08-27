# Task 8 Fix Round 3 Scoped Re-review

## Verdict

- **Specification: PASS.** The five scoped Round 2 findings (`C1`, `C2`, `I5`, `I6`, `N1`) are addressed by the current implementation and the reported fake-only regression coverage.
- **Standards/security: PASS for the scoped review.** Proposal/result publication is fail-closed, apply ownership and recovery are durable, filesystem writes are confined to pinned directory inodes, and persisted journal/receipt data is bound and integrity-checked.
- **Overall: ACCEPTED for Task 8 scope.** No new Critical or Important regression was introduced by Round 3. This review does not authorize local BGW/XFlow execution or live external writes.

## Verification Boundary

This was an independent static review of the Round 3 files, cumulative report, prior review, brief, and fake test evidence. Per the review package, no tests were rerun, no Git operation was performed, and no iCafe, KU, Infoflow, iCode, iPipe, build, compiler, unit, regression, integration, Docker, NCS, simulator, or release command was executed. The implementation report records `29` focused tests and `405` full control-plane tests passing, schema `14/14`, and `compileall` passing. Its current Round 3 hashes match the files under review:

```text
49824617e789f10b0ab246b1540e2f1e15687ef63e6cda3c9b945c0a5a4aa443  scripts/run_summary.py
0bcd989d6fcfaeef3059e8f4a83adf985ee82ef47025d4bcb1d10c59fae0bae  scripts/state_store.py
1a788f1efdb893293cd092d0f3f81323fdcd49abf06ef5a5c723cfc59b2c684e  scripts/tests/test_run_summary.py
```

## Finding Matrix

| ID | Verdict | Static evidence |
|---|---|---|
| C1 | **ADDRESSED** | Proposal rows start in `ARCHIVING` and become `PROPOSED` only after `_receipt_valid()` and `mark_optimization_archived()`. Apply requires that verified proposal receipt. Result publication is persisted as `RESULT_ARCHIVING`/`ARCHIVE_PENDING`; terminal `APPLIED`/`ROLLED_BACK` is written only by `complete_optimization_result_archive()` after a verified result receipt. (`scripts/run_summary.py:65-119,205-211,331-350`; `scripts/state_store.py:523-560,625-657`) |
| C2 | **ADDRESSED** | `claim_optimization_apply()` provides a CAS owner token, PID, heartbeat and lease. Fresh/live/ambiguous owners are not recovered. Rollback failure enters nonterminal `RECOVERY_REQUIRED`; retry verifies the durable journal, restores bytes, then archives the rollback result. (`scripts/run_summary.py:124-211`; `scripts/state_store.py:562-668`) |
| I5 | **ADDRESSED** | Baseline reads and writes use root/parent descriptors opened with `O_DIRECTORY|O_NOFOLLOW`. Root and parent device/inode identities are recorded and checked again immediately before replacement/unlink, including recovery, preventing a checked-parent-to-new-directory rebound. (`scripts/run_summary.py:264-323,377-453`) |
| I6 | **ADDRESSED** | Structured JSON keys/values plus escaped quoted values, headers, URL credentials, query secrets, email and phone forms share redaction and candidate rejection. The Round 3 multiline/escaped/header/URL fake test exercises the previously leaking forms. (`scripts/run_summary.py:15-20,246-262,458-511`; `scripts/tests/test_run_summary.py:502-508`) |
| N1 | **ADDRESSED** | The v2 journal binds proposal ID, run ID, candidate hash, approval, canonical allowed roots and ordered target identities. StateStore adds an owner proof and canonical journal hash; claim and recovery verify the hash, owner/lease claim, roots, exact target order, and every decoded backup hash before any rollback write. (`scripts/run_summary.py:283-309`; `scripts/state_store.py:571-599,720-731`) |

## Regression Assessment

No new Critical or Important regression was found in Round 3. The only residual operational concern is intentional: a failed KU result archive remains visibly nonterminal in `ARCHIVE_PENDING` and requires a later configured KnowledgeSync retry. This is fail-closed and does not claim a terminal result or perform additional business-repository, profile, pipeline, or external delivery side effects.

Task 8 may hand its reviewed output to the authorized Task 9/10 continuation, subject to their independent review and the same no-local-project-execution boundary.
