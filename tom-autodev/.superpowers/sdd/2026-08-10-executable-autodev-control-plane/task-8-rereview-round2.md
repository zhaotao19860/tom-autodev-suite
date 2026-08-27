# Task 8 Fix Round 2 Scoped Re-review

## Verdicts

- **Specification: FAIL.** `N2` and `N3` are addressed, but `C1`, `C2`, `I5`, `I6`, and `N1` remain open.
- **Standards/security: FAIL.** Proposal `ARCHIVING` and lease liveness improved, but result receipts are not durable/verified, rollback failure is falsely terminal, parent replacement remains racy, and the journal self-hash does not bind it to the approved operation.
- **Overall: BLOCKED / FIX ROUND 3 REQUIRED.** Task 8 cannot be accepted or handed to Task 9/10.

## Verification

- Reviewed the round-2 package, brief, initial/round-1 reviews, cumulative report, and the exact three round-2 files.
- Final round-2 SHA-256 values in `task-8-report.md` match the current files.
- `python3 -m unittest scripts/tests/test_run_summary.py -v`: **22/22 PASS**.
- `python3 -m unittest discover -s scripts/tests -q`: **398/398 PASS**.
- `python3 -m compileall -q scripts`: **PASS**.
- All additional checks used temporary SQLite/artifact roots, fake KnowledgeSync/validation dependencies, and mocks only. No live external action or project build/test command ran.

## Finding Matrix

| ID | Verdict | Evidence |
|---|---|---|
| C1 | **NOT ADDRESSED** | Proposal crash ambiguity is fixed with `ARCHIVING`, but failed/rollback/recovery result publication is still not durably completed, and an unbound `{ok: true}` proposal receipt authorizes apply (`scripts/run_summary.py:65-74`, `scripts/run_summary.py:85-86`, `scripts/run_summary.py:113-119`, `scripts/run_summary.py:155-164`). |
| C2 | **NOT ADDRESSED** | Lease/PID gating now fails closed for fresh/live/ambiguous owners, but rollback failure still writes terminal status `ROLLED_BACK` and can report a false `...ROLLED_BACK` reason while candidate bytes remain (`scripts/run_summary.py:113-119`). |
| I5 | **NOT ADDRESSED** | `O_NOFOLLOW` blocks symlink traversal, but baseline validation and parent-fd acquisition are still separate. Replacing the checked parent with another real directory makes apply return `OK` against a different directory inode (`scripts/run_summary.py:106-107`, `scripts/run_summary.py:230-234`, `scripts/run_summary.py:280-297`). |
| I6 | **NOT ADDRESSED** | The prior single-token quoted JSON example is fixed, but quoted multiword secrets are only partially redacted; `password="hello world"` becomes `[REDACTED_SECRET] world"` (`scripts/run_summary.py:20-23`, `scripts/run_summary.py:324-326`). |
| N1 | **NOT ADDRESSED** | Journal confinement is improved, but the journal is only self-hashed. `proposal_binding` is never checked against the loaded proposal/approval/owner, so a recomputed journal can restore attacker bytes to an unrelated file inside an allowed root (`scripts/run_summary.py:217-228`, `scripts/run_summary.py:236-248`). |
| N2 | **ADDRESSED** | Reject terminalization now persists `approval_id`; same-approval replay returns the original reject and a wrong approval is denied (`scripts/run_summary.py:88-96`, `scripts/run_summary.py:146-153`; `scripts/state_store.py:623-646`). |
| N3 | **ADDRESSED** | Narrative fields, target list, and verification list now receive explicit type/nonempty checks and malformed versioned candidates return `G10_CANDIDATE_INVALID` (`scripts/run_summary.py:199-215`). |

## Open Details

### C1 - Mandatory result receipts and archive recovery are incomplete

The new proposal flow correctly inserts `ARCHIVING`, blocks apply in that state, stores a proposal receipt, and marks `PROPOSED` only afterward (`scripts/run_summary.py:65-86`; `scripts/state_store.py:521-560`). A cancellation reproduction left `ARCHIVING`, and later `apply()` returned `G10_ARCHIVING`; the original proposal crash window is closed.

However, after any apply failure, `_archive()` is called and its result is discarded before `finalize_optimization_apply(..., "ROLLED_BACK", ...)` (`scripts/run_summary.py:113-119`). Dead-owner recovery does not call `_archive()` at all before generic terminal update (`scripts/run_summary.py:155-164`). With a result-only failing KnowledgeSync, both result attempts failed, yet the row became `ROLLED_BACK` and no durable result receipt/handoff existed. A success-result receipt is likewise not stored with `APPLIED`; only the earlier proposal receipt remains.

Proposal receipt validation is also too weak: `_archive()` accepts any mapping with `ok`, `mark_optimization_archived()` persists it, and `apply()` checks only `archive_receipt.ok` (`scripts/run_summary.py:67-74`, `scripts/run_summary.py:85-86`, `scripts/run_summary.py:255-260`; `scripts/state_store.py:550-560`). A fake returning only `{ok: true}` produced `PROPOSED` and a successful apply. Validate and bind run ID, exact artifact/content hash, KU/iCafe identities, and evidence refs; persist separate proposal/result receipts and archive-pending states so a failed result archive remains recoverable rather than terminal.

### C2 - Rollback failure still claims a completed rollback

The lease claim now atomically records heartbeat/expiry, owner token, and PID (`scripts/state_store.py:562-593`). `_recover_or_wait()` waits on a fresh lease and on live/ambiguous PID status; only expired plus conclusively absent PID reaches rollback (`scripts/run_summary.py:155-164`, `scripts/run_summary.py:335-353`). Those live/dead/fresh semantics are addressed.

The decisive failure path is unchanged. If `_rollback_journal()` returns false after a validation `_ApplyFailure`, line 115 keeps `G10_VALIDATION_FAILED_ROLLED_BACK`; line 118 still finalizes `ROLLED_BACK`. The reproduction left the target at `after`, returned `G10_VALIDATION_FAILED_ROLLED_BACK`, and stored `ROLLED_BACK`. A parent-swap failure returned `G10_ROLLBACK_FAILED` but was also stored as `ROLLED_BACK`. Keep `APPLYING` or move to a nonterminal `ROLLBACK_FAILED/RECOVERY_REQUIRED` state with the authenticated journal intact; never emit rollback-success wording or terminalize until bytes are verified restored.

The lease tests exercise public `RunSummary.apply()` for recovery and public `heartbeat()` for renewal, although their setup directly calls `StateStore.claim_optimization_apply()`. The static lease checks match the claimed fresh/live/dead/ambiguous behavior; no separate lease blocker was found.

### I5 - Dirfd traversal still does not bind validation to the written parent inode

Opening each component with `O_DIRECTORY|O_NOFOLLOW` and using fd-relative replace/unlink prevents the prior symlink escape (`scripts/run_summary.py:272-307`). In the symlink-swap reproduction, nothing was created outside `control_root`.

It does not prevent a rename/replacement with a normal directory. After `_revalidate_target()` read the approved baseline, replacing `scripts/` with a fresh real `scripts/` before `_atomic_write()` made the dirfd traversal accept the new directory. Apply returned `OK`; the new `scripts/guard.py` contained `after`, while `scripts-original/guard.py` retained `before`. Open/pin the parent directory fd before reading the baseline/journaling, record its device/inode identity, and use that same verified fd for candidate write and rollback.

### I6 - Multiword quoted values leak suffixes

`_QUOTED_SECRET` fixes quoted Authorization/API-key tokens and `_has_secret()` rejects them in candidate content. The value matcher still stops at whitespace, so quoted passwords/credentials containing spaces leak all text after the first word in summary/KU redaction. Match the complete quoted value (with escape handling) or parse structured strings with a proper parser, then add multiword, escaped-quote, header, and URL cases.

### N1 - Journal hash is not an operation binding

The journal now contains canonical roots, a `proposal_binding`, and a `journal_hash`; rollback validates the self-hash and confines paths (`scripts/run_summary.py:217-248`). This blocks the prior outside-root journal path when the hash is not recomputed.

But recovery never compares `proposal_binding` to the stored proposal candidate/roots and the binding omits proposal ID, approval ID, owner token, and entry identities. Replacing the journal with an unrelated in-root entry and recomputing its public SHA-256 passed validation; expired/dead recovery overwrote `unrelated.py` with attacker bytes and returned `G10_INTERRUPTED_ROLLED_BACK`. Bind the journal to the exact proposal/candidate hash, approval and owner claim in the claim transaction, verify the entry set exactly matches candidate targets, verify each decoded-byte hash, and use the verified binding during rollback.

## New Findings

No additional Critical/Important finding was opened beyond the five unresolved scoped findings above. The unused `StateStore.recover_optimization_apply()` still exposes pre-lease PID-only semantics (`scripts/state_store.py:606-611`); update or remove it during round 3 to avoid a future inconsistent recovery caller.

## Test Assessment

The round-2 tests now cover the exact quoted single-token example, malformed candidate, reject replay, heartbeat token, and basic lease outcomes. They do not cover result-receipt persistence/failure recovery, rollback failure truthfulness, recomputed/cross-proposal journal binding, real-directory parent replacement, or multiword quoted secrets. These need regression tests in round 3.
