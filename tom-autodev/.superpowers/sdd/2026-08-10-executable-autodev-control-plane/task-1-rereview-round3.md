# Task 1 Re-Review: Fix Round 3

## Verdict

- Spec: **FAIL**
- Quality: **FAIL**

## Verified Behavior

- **Immediate retries work.** A successful event stores an operation identity with source state, target state, input hash, and selected gate (`scripts/orchestrator.py:105` through `scripts/orchestrator.py:114`). When the latest committed event is that exact target/identity, the retry path returns the persisted result (`scripts/orchestrator.py:80` through `scripts/orchestrator.py:86`, `scripts/orchestrator.py:200` through `scripts/orchestrator.py:224`). The test proves no duplicate event for `GRILL -> SPEC` retry (`scripts/tests/test_orchestrator.py:140` through `scripts/tests/test_orchestrator.py:169`).
- **The previous cross-source collision is fixed.** A later `DIAGNOSE -> SPEC` with the same target/hash does not reuse a prior `GRILL -> SPEC` result, is blocked until G6 is approved, then creates a different event (`scripts/tests/test_orchestrator.py:171` through `scripts/tests/test_orchestrator.py:218`).

## Remaining Finding

### Critical: Re-entering the same source state reuses a previous advance result

- Axis: Spec and Standards
- Blocking: yes
- Evidence: After the narrow immediate-retry check, `advance()` constructs an identity from only source state, target state, input hash, and gate (`scripts/orchestrator.py:88` through `scripts/orchestrator.py:92`; `scripts/orchestrator.py:190` through `scripts/orchestrator.py:198`) and unconditionally returns any matching historical idempotency result (`scripts/orchestrator.py:93` through `scripts/orchestrator.py:95`). It lacks the source checkpoint/event identity.
- Reproduction: complete `GRILL -> SPEC` with hash `H`; later re-enter `GRILL` through a legal repair/architecture loop; then request `GRILL -> SPEC` with `H`. The latest state is `GRILL`, so `_retry_operation_identity()` correctly returns `None` because it is not a retry of a last `SPEC` event. The fallback at lines 93-95 nevertheless finds the original `GRILL/SPEC/H/G1` key and returns its old successful result. No event is appended, the state remains `GRILL`, and the current G1 approval/evidence is not checked.
- Affected acceptance criteria: idempotency is preserved, transitions are fail closed, and approved evidence is bound to the actual operation.
- Required correction: include the persisted source checkpoint/event ID (or a monotonically increasing transition-attempt epoch) in the operation identity and idempotency key. Retain the existing last-event retry branch for true retries. Add a regression test for an intervening legal loop that re-enters the same source, asserting the old result is not reused and the new gate/evidence is evaluated.

## Assessment

Round 3 addresses the original different-source collision and supports true immediate retries, but does not provide complete operation collision safety across repeated entries into the same phase. No tests were rerun; the report's recorded test results were not independently verified.
