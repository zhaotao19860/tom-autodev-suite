# Task 8 Independent Review

## Verdicts

- **Specification: FAIL.** Mandatory KU archival can be bypassed, G10 application is not failure-atomic, persisted proposal tampering can change the approval run, successful runs are summarized incorrectly, and summaries are not stable/idempotent.
- **Standards/security: FAIL.** Exception/crash/concurrency recovery, immutable-envelope verification, symlink-root rejection, and secret redaction are incomplete.
- **Overall: BLOCKED / CHANGES REQUIRED.** Task 8 must not be marked complete or feed Task 9/10 until all Critical and Important findings are fixed and independently re-reviewed.

## Verification Performed

- Reviewed `task-8-brief.md`, `task-8-report.md`, the progress ledger, and every file listed in the review package.
- All seven reported SHA-256 values match the current files.
- `python3 -m unittest scripts/tests/test_run_summary.py -v`: **14/14 PASS**.
- `python3 -m unittest discover -s scripts/tests -v`: **390/390 PASS**.
- `python3 -m compileall -q scripts`: **PASS**.
- Safety scan found no Codex compatibility, `tom-autorelease` runtime import, shell execution, or prohibited local project command invocation in the changed production files. The only command-name matches are the rejection allowlist itself.
- All additional reproductions used temporary SQLite stores, temporary artifact roots, fake/injected dependencies, and temporary files. No live iCafe/KU/Infoflow/iCode/iPipe write or BGW/XFlow build/test operation was run.

## Critical Findings

### C1 - Mandatory KnowledgeSync archival is bypassable and archive exceptions leave an applyable proposal

`RunSummary._archive()` returns success with `skipped=True` when no `knowledge_sync` is supplied (`scripts/run_summary.py:322-324`), while both `RunSummary.__init__()` and `Orchestrator.run_summary()` make that dependency optional (`scripts/run_summary.py:29-44`, `scripts/orchestrator.py:135-144`). Consequently `propose()` persists a `PROPOSED` record and reports success, and `apply()` modifies files and reports success, without publishing either proposal or result to KU. A temporary-store reproduction returned `missing_sync_propose=True` and `missing_sync_apply=True`, with the target changed to the candidate bytes.

The inverse failure is also unsafe: `propose()` saves `PROPOSED` before calling `_archive()` and does not catch exceptions from `publish_phase()` (`scripts/run_summary.py:128-140`, `scripts/run_summary.py:322-328`). A fake that raises `RuntimeError` leaves the database row in `PROPOSED`, rather than terminal `ARCHIVE_FAILED`. Rejected and rolled-back results also ignore the archive return/exception before making the proposal terminal (`scripts/run_summary.py:155-159`, `scripts/run_summary.py:180-192`). This violates the requirement that proposal/result archival is mandatory and only goes through KnowledgeSync. Require a verified KnowledgeSync receipt before an applyable status exists, convert dependency exceptions to stable fail-closed results, and journal/retry result archival idempotently.

### C2 - Candidate application is not atomic or recoverable across validation exceptions, process loss, or concurrent callers

`apply()` writes every target before invoking validation (`scripts/run_summary.py:164-179`) but rolls back only a synthetic `_ValidationFailure` or `OSError` (`scripts/run_summary.py:180-193`). The default runner can raise `subprocess.TimeoutExpired`, and injected runners/archivers can raise other exceptions. A reproduction whose validation runner raised `TimeoutExpired` left the target at `after` and the proposal at `PROPOSED`. `_rollback()` can itself raise and is not guarded (`scripts/run_summary.py:330-335`).

Backups and progress exist only in memory; there is no durable `APPLYING` record or rollback journal. A process exit between writes, validation, result archival, and the final `APPLIED` update (`scripts/run_summary.py:175-205`) leaves modified or partially modified files with a replay that can only report baseline mismatch. There is also no compare-and-swap ownership claim: two callers can both read `PROPOSED` (`scripts/run_summary.py:143-151`), validate the same baseline, and duplicate validation/archive side effects; `StateStore.update_optimization_proposal()` serializes only each final SQL update, not the filesystem transaction (`scripts/state_store.py:558-581`). Add a durable single-owner apply state and exact backup journal before the first write, recover it on restart, catch all dependency failures while preserving rollback evidence, and cover interruption/concurrency tests.

### C3 - Proposal-envelope tampering defeats G10 run binding, and terminal replay bypasses approval validation

The database stores an unhashed proposal JSON blob plus a candidate hash (`scripts/state_store.py:519-546`). Loading it performs no integrity or column/payload cross-check (`scripts/state_store.py:548-556`, `scripts/state_store.py:624-634`). `apply()` recomputes only the nested candidate hash, then trusts the mutable payload's `run_id` for approval validation and archival (`scripts/run_summary.py:151-163`, `scripts/run_summary.py:309-328`); it does not recompute `proposal_id = hash(run_id:candidate_hash)` or compare payload run/hash with the immutable columns.

In a temporary database, changing only `payload_json.run_id` from `run-1` to `run-2`, then supplying a valid run-2 G10 approval for the unchanged candidate hash, returned `OK` and applied the run-1 proposal. The current tamper test changes candidate content only (`scripts/tests/test_run_summary.py:200-215`) and misses this binding attack. Additionally, terminal status is returned before loading or validating the caller's approval (`scripts/run_summary.py:147-153`); replaying an applied proposal with `not-an-approval` returned the prior `OK`. Hash the complete immutable proposal envelope, validate it and all denormalized columns/proposal ID on every load, persist the effective `approval_id`, and authenticate terminal replays against the original operation identity.

## Important Findings

### I1 - The real successful terminal state is never classified as success

`build()` recognizes `RELEASE` and nonexistent `COMPLETED` as success, but the actual state machine defines `RELEASE_SUCCESS` as the terminal success state and `RELEASE` as a nonterminal predecessor (`scripts/run_summary.py:54-57`, `scripts/transition_policy.py:17-26`, `references/state-machine.md:5-8`). A temporary `RELEASE_SUCCESS` run was summarized as `IN_PROGRESS`. Historical failures also override a later successful repair/release because `failures` is tested before terminal success. This breaks the required successful/failed/timeout summaries. The required successful-run test is absent; the focused tests cover only failed and timeout outcomes (`scripts/tests/test_run_summary.py:96-134`).

### I2 - Rebuilding a summary changes its own metrics and hash forever

`build()` queries all run artifacts, includes their count in the summary, and then writes another `run-summary` artifact (`scripts/run_summary.py:46-65`, `scripts/run_summary.py:87-93`). The next build therefore counts the prior summary. Two unchanged builds in a temporary store produced artifact counts `0` then `1` and different content hashes. This violates stable metrics and idempotency and makes repeated proposal generation conflict because the proposal ID is candidate-stable while `summary_hash` and `summary_artifact_id` drift. Exclude summary/G10 artifacts from source metrics or replay a summary keyed to a stable source-evidence snapshot.

### I3 - Summary/candidate extraction does not match the production event envelope

The production failure route stores details under `payload.evidence` (`scripts/orchestrator.py:267-305`), but `_failure_groups()` reads `failure_signature` and `message` only at payload top level and recognizes `...FAILURE`, `REMOTE_TIMEOUT`, or `REVIEW_FAILURE`, not production reasons such as `REVIEW_FAILED`/`RELEASE_FAILED` (`scripts/run_summary.py:207-218`). `_optimization_candidates()` likewise reads only a top-level `optimization_candidate` (`scripts/run_summary.py:228-235`). Repository search shows no production writer for that field; only `test_run_summary.py` injects it directly with `StateStore.transition()` (`scripts/tests/test_run_summary.py:90-94`). Normal controller evidence will therefore lose stable signatures and cannot reach proposal generation. Define and consume one versioned production candidate envelope (or derive it from archived diagnosis/summary evidence), including nested routed evidence and the complete failure taxonomy.

### I4 - `propose()` accepts fabricated, unarchived summary input

The only summary validation is `ok=True` plus a string `run_id` (`scripts/run_summary.py:95-100`). It never loads `artifact_id`, verifies artifact kind/integrity, recomputes `content_hash`, or checks that evidence matches the durable run. A reproduction using `{ok: true, run_id: "run-1", content_hash: "forged"}` and no artifact ID returned `OK`, persisted `summary_hash="forged"`, and archived forged failure evidence. This contradicts “only a proposal created from archived run evidence may be applied” and weakens G10 review context even when the candidate hash itself is correct. Bind proposal creation to an integrity-checked `run-summary` artifact and reject all caller-supplied summary fields that do not match it.

### I5 - A symlink supplied as an allowed root is accepted

`_allowed_roots()` resolves each root before calling `is_symlink()` (`scripts/run_summary.py:248-262`), so the check is made on the resolved target, not the supplied path. A symlink root pointing to a real directory under `control_root` returned proposal `OK`. The target-symlink test (`scripts/tests/test_run_summary.py:159-168`) does not cover root or ancestor symlinks. Validate the unresolved path and every relevant path component, pin root identity, and revalidate immediately before each atomic write to close symlink/TOCTOU paths.

### I6 - Secret and personal-data redaction misses common forms and can publish the remaining secret

The regex at `scripts/run_summary.py:20` consumes only the first whitespace-delimited value after `authorization:`. `_redact("Authorization: Bearer actual-secret api_key=second-secret token third-secret")` produced `[REDACTED_SECRET] actual-secret api_key=second-secret token third-secret`. It also does not cover API-key spellings or non-email PII, while `_archive()` serializes the redacted value to KnowledgeSync (`scripts/run_summary.py:322-328`, `scripts/run_summary.py:391-403`). The current test covers only `token=...` and one email (`scripts/tests/test_run_summary.py:96-113`). Use the established canonical redaction/persistence policy for arbitrary strings, reject secret-bearing candidate content before persistence, and add boundary-split/Bearer/API-key/credential/phone tests.

## Minor Findings

### M1 - Atomic replacement changes file metadata

`_atomic_write()` creates a fresh `mkstemp` file and replaces the target without preserving mode bits (`scripts/run_summary.py:343-356`); rollback uses the same helper. Editing or rolling back an executable control-plane script can leave exact bytes but remove execute permissions. Preserve and restore required metadata, or explicitly reject targets whose metadata cannot be safely retained.

### M2 - The implementation report's full-suite count is stale

`task-8-report.md:50-53` records 389 tests; the current exact suite ran 390. File hashes are correct and this does not affect runtime behavior, but the independent-review baseline should record the current count after fixes. The initial import-only RED is disclosed honestly; the later claimed behavioral REDs have no command/output transcript and should be captured in the repair report.

## Test and Scope Assessment

- Existing tests adequately exercise the happy apply path, returned validation failure, returned KnowledgeSync failure, direct target symlinks, explicit forbidden targets, command allowlisting, and candidate-content tampering.
- They do not exercise `RELEASE_SUCCESS`, repaired-success history, repeat-build stability, absent/raising KnowledgeSync, validation timeout/exception, rollback exception/process interruption, concurrent apply, full-envelope/run tampering, unauthorized terminal replay, symlink roots/ancestors, forged summary artifacts, production nested failure envelopes, or broader redaction forms.
- `test_fake_e2e.py` does not exist. This is **not a separate Task 8 blocker** because Task 10 explicitly owns creation of BGW/XFlow fake-E2E scenarios and orchestration wiring. Task 8 still must supply the unit/contract behavior Task 10 will consume; the findings above show that it currently does not.

## Positive Controls Confirmed

- Candidate commands are parsed with `shlex.split`, require `python3 -m unittest`, and reject shell metacharacters and project command families (`scripts/run_summary.py:337-348`, `scripts/run_summary.py:405-422`).
- Canonical target paths are confined to resolved control-plane roots and explicit business/iPipe/pipeline/profile path names are rejected in the tested direct-path cases (`scripts/run_summary.py:264-307`).
- ApprovalLedger itself requires both Comate and Infoflow delivery receipts and authorized responders before creating an effective strict approval (`scripts/approval_ledger.py:187-199`); C3 is in proposal-envelope/replay validation, not that ledger rule.
- No Task 8 code introduces Codex support, a `tom-autorelease` runtime dependency, or local BGW/XFlow execution.
