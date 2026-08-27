# Task 7 Fix Round 3 Re-review

Date: 2026-08-11

## Review Basis

- Scope: the exact round-3 package, `task-7-brief.md`, the `Task 7 fix round 3` section of the cumulative implementation report, and `task-7-rereview-round2.md`.
- Round-3 files: `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py`, `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task7_controller_boundary.py`, and `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_phase_protocol_repair.py`.
- Method: static production/test inspection and SHA-256 checks only. No tests, compilation, project commands, live adapter calls, iCode/iPipe/KU/iCafe writes, or implementation edits were performed.
- Prior scoped review SHA-256: `53f3a63873dc116ac4f514f7d30a7b45bfd7fc0a9f9d528dad34f72d0356ca7e`.
- Cumulative implementation report SHA-256: `4594445087a6a25bfd894bf0b7d5d8963cbdccd4a7d26856b94a820f80440866`.
- All three round-3 file hashes match the cumulative implementation report: `orchestrator.py` `2ed1d2f4cb875a64ac971d5a88017c269e97fcd30bb33e95e9e227edde32d954`, `test_phase_protocol_repair.py` `e90f2d741b7da2f14dd02c5518c803ba0e8e038be334ad53933e937ddcfe6d38`, and `test_task7_controller_boundary.py` `4cf5d02cbcdd8d8617657a11922233d28ada8392ddbdff35df8b43aefcb9e1f5`.
- The supplied `313/313` focused result, `376/376` full-suite result, compileall, schema, and safety scans are evidence only and were not rerun.

## Verdicts

- Specification: **PASS**
- Standards: **PASS**
- Overall: **PASS**
- Gate: **PASS**; no blocking confirmed finding remains from this scoped round.

The three scoped findings are addressed. No new Critical or Important breakage was found in the round-3 files. M1 remains deferred outside this loop.

## Disposition Summary

| ID | Disposition |
| --- | --- |
| I4 | **ADDRESSED** |
| I8 | **ADDRESSED** |
| N2 | **ADDRESSED** |

Addressed IDs: `I4`, `I8`, `N2`.

Open IDs: none.

New Critical/Important IDs: none.

## Findings

### I4 - Owned iPipe binding has no executable production event-formation path

**ADDRESSED** (Specification).

`Orchestrator.submit_to_ipipe()` now owns the SUBMIT-to-IPIPE boundary at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:433` through `:587`:

- It requires a run-bound runtime and a `SUBMIT` checkpoint, invokes the runtime, and requires the exact durable `icode.submit` intent/receipt to match the returned receipt at `:468` through `:513`.
- It validates the run-bound G7 record and candidate input hash at `:514` through `:525`, reloads the pinned profile at `:526` through `:535`, and derives the profile-owned pipeline/module/release/environment/revision binding through `_submission_controller_binding()` at `:905` through `:961`.
- It stores the canonical verified iCode receipt as a durable `submission` artifact with exact `metadata.controller_binding` at `:537` through `:545`.
- It places the complete binding, submission ID/hash, and G7 identity at the IPIPE event payload top level and uses `commit_transition_result()` for the CAS checkpoint at `:557` through `:587`.

This is the production ownership path consumed by `PhaseProtocol.next()` and `ingest_ipipe_evidence()`. Caller data cannot choose the profile-owned pipeline ID, module, release rule, environment fingerprint, or normalized source revisions; those values are derived from the pinned profile and verified receipt.

### I8 - iPipe repair coverage bypasses the production boundary

**ADDRESSED** (Standards).

The new focused fixture starts a run through strict `Orchestrator.start()` at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task7_controller_boundary.py:161` through `:180`, reaches `SUBMIT` through public `Orchestrator.advance()` transitions at `:225` through `:250`, and obtains the IPIPE event and submission predecessor only through `submit_to_ipipe()` at `:252` through `:260`.

The production-bound tests assert the owned artifact/top-level binding at `:269` through `:297`, stable runtime-exception failure at `:299` through `:311`, successful ingestion and evidence/next-action propagation at `:313` through `:329`, owned content mismatches and submission/G7 failures at `:331` through `:364`, nested secret-bearing evidence rejection at `:365` through `:383`, exact historical replay/corruption at `:385` through `:414`, and pinned-profile drift at `:416` through `:429`. There is no direct `StateStore.transition(..., "IPIPE", ...)` in the new controller-bound test module. The remaining manually seeded IPIPE artifact in the repair suite at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_phase_protocol_repair.py:906` through `:920` is only a predecessor for the RELEASE-descriptor test; it does not test IPIPE ingestion or ownership and does not bypass the boundary under review.

### N2 - Advertised CLI `start` cannot supply the required snapshot

**ADDRESSED** (Specification and Standards).

The CLI now captures the established snapshot through `CafeClient.snapshot()` and passes the raw canonical snapshot to strict `Orchestrator.start()` at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:992` through `:1008`. Adapter failures become structured non-ready results, and `_cli_exit_code()` returns 1 for `ready: false`, `ok: false`, `RUN_NOT_FOUND`, and every non-success reason at `:1021` through `:1029`.

The focused parser/main tests use an injected fake Cafe adapter at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task7_controller_boundary.py:432` through `:455`, prove valid snapshot capture and INTAKE persistence at `:457` through `:464`, and prove missing/invalid snapshot failures return exit code 1 at `:465` through `:474`.

## New-Breakage Review

No new Critical or Important finding was identified in the round-3 production path or focused tests. The controller keeps strict pinned-profile, durable-intent, approval, source-revision, and evidence checks; exception conversion is stable; tests remain local/fake-boundary only; and the CLI failure path is now fail-closed with a nonzero exit.

## Axis Reports

- **Specification: PASS.** I4 and N2 are addressed with executable production paths and exact binding/CLI behavior.
- **Standards: PASS.** I8 is addressed with controller-bound integration-shaped tests and no prohibited live/project command dependency in the round-3 files.
- **Overall/Gate: PASS.** No blocking confirmed finding remains in this scoped re-review, so the Change Set is eligible for the next approval gate subject to the supplied verification evidence.

## Verification Limits

- `UNVERIFIABLE_BY_REVIEW_INSTRUCTION`: supplied test, compile, schema, and safety-scan results were not rerun.
- `UNVERIFIABLE_LIVE`: no live iCafe, iCode, iPipe, KU, or external controller operation was independently established.
- M1 remains deferred and is not a condition of this round's verdict.
