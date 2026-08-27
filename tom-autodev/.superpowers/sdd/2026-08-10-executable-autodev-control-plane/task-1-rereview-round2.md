# Task 1 Re-Review: Fix Round 2

## Verdict

- Spec: **FAIL**
- Quality: **FAIL**

## Verified Corrections

- **G6 semantics are correct.** Automatic `route_failure()` into `DIAGNOSE` remains ungated (`scripts/orchestrator.py:112` through `scripts/orchestrator.py:148`). The two non-terminal exits from `DIAGNOSE` select G6 and require a `failure-bundle` (`scripts/evidence_policy.py:38` through `scripts/evidence_policy.py:42`). `advance()` supplies its actual current state to both ledger lookup and evidence validation (`scripts/orchestrator.py:88` through `scripts/orchestrator.py:89`). Coverage proves missing/wrong G6 blocks and a ledger-approved G6 succeeds, while failure routing remains automatic (`scripts/tests/test_orchestrator.py:177` through `scripts/tests/test_orchestrator.py:224`).
- **G8 semantics are correct.** Normal `SUBMIT -> IPIPE` retains target-state G7; only `ENVIRONMENT_BLOCKED -> IPIPE` overrides it to G8 (`scripts/evidence_policy.py:25` through `scripts/evidence_policy.py:26`, `scripts/evidence_policy.py:38` through `scripts/evidence_policy.py:42`). The tests reject G7 for recovery and accept G8 (`scripts/tests/test_orchestrator.py:272` through `scripts/tests/test_orchestrator.py:323`).
- **Empty-evidence coverage is complete for the operational non-INTAKE phases.** The test now includes `ARCHITECTURE_REVIEW` and `ENVIRONMENT_BLOCKED` (`scripts/tests/test_gates.py:53` through `scripts/tests/test_gates.py:75`). `STOPPED` is terminal rather than an advanceable phase and is covered by the terminal-operation tests.

## New Finding

### Critical: Advance idempotency key conflates different source-state transitions

- Axis: Spec and Standards
- Blocking: yes
- Evidence: `scripts/orchestrator.py:74` through `scripts/orchestrator.py:78` form and consult the idempotency key as `advance:{run_id}:{next_state}:{input_hash}` before reading the current state or validating the transition. It omits `current_state`, even though phase requirements are now source-transition sensitive (`scripts/evidence_policy.py:38` through `scripts/evidence_policy.py:42`).
- Impact: after a prior `GRILL -> SPEC` with input hash `H`, a later `DIAGNOSE -> SPEC` with the same `H` returns the old successful `SPEC` result at line 78 before it checks G6, current state, or required repair artifacts. The persisted state stays `DIAGNOSE`, while the response falsely reports success. This is a fail-open response for a distinct operation and violates the idempotency/transition contract.
- Required correction: include the source state and a canonical identity of the transition evidence/operation in the advance idempotency key, and compute it only after the current state is known. Add a regression test covering a repeated target/hash from a different source state, asserting that G6 is still checked and state/result remain consistent.

## Assessment

The round-two transition-aware rules resolve the prior G6/G8 and coverage findings. No additional Critical or Important breakage was introduced in round two. The unresolved Critical idempotency collision was introduced by the earlier repair's change from a source-state-aware advance key and remains in the current Task 1 implementation. Tests were not rerun; the implementation report's recorded results were not independently verified.
