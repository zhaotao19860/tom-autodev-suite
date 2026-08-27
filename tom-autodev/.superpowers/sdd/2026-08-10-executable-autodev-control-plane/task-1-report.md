# Task 1 Fix Report: Close Evidence and Transition Gates

## Scope

Implemented the first review-fix round only in Task 1 control-plane files:

- `scripts/approval_ledger.py`
- `scripts/evidence_policy.py`
- `scripts/evidence_gate.py`
- `scripts/transition_policy.py`
- `scripts/orchestrator.py`
- `scripts/tests/test_gates.py`
- `scripts/tests/test_orchestrator.py`

No BGW/XFlow build or test command was run.

## TDD Record

RED command:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Initial result: 24 tests ran; 9 failed and 2 errored. The failures demonstrated the reviewed defects: caller-forged approval accepted, empty revisions accepted or masked, malformed timestamps raised `ValueError`, terminal operations wrote or returned the wrong policy block, successful events omitted policy decisions, and repeated transitions were not idempotent.

GREEN command:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Result: 25 tests passed.

Required full verification:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result: 46 tests passed in 0.286 seconds.

## Implemented Corrections

- `Orchestrator.advance()` now resolves the supplied `approval_id` from `ApprovalLedger`; caller-supplied approval evidence is overwritten and cannot authorize a transition. The gate binds the ledger record's action, ID, approved decision, and exact canonical input hash.
- Explicit generic requirements now model every human gate from G0 through G10. Phase requirements continue to map their required gate, artifact evidence, revisions, and environment fingerprints.
- Release revision evidence must be non-empty schema-valid mappings. Repository names and revisions must be non-empty strings; when `required_repositories` is provided, both evidence sets must contain all required repositories before comparison.
- `TransitionPolicy` explicitly models `RELEASE_SUCCESS` and `STOPPED` as terminal states and returns the stable `TERMINAL_STATE` block. `stop()` now uses the transition policy and does not write events for terminal runs.
- Successful advance, failure-route, and stop events persist a stable `policy_decision`, including `policy_id`, current state, target state, decision result, and reason code.
- Advance and failure idempotency keys are checked before revalidating the post-transition state, so repeated identical successful calls return the persisted result without another event.
- Invalid, non-string, or timezone-naive timestamps return the structured `INVALID_TIMESTAMP` gate block rather than raising.

## Review-Finding Closure

| Finding | Closure |
|---|---|
| Forged approvals; missing G6/G8/G10 | Ledger-backed approval lookup plus positive/mismatched coverage for G0-G10. |
| Empty release revisions | Non-empty, typed, required-repository revision validation and empty/None/partial/mismatch tests. |
| Terminal `stop()` write | Explicit terminal policy and no-write terminal-operation tests for both terminal states. |
| Missing policy decision event evidence | Persisted decision payloads asserted for advance and failure routing. |
| Malformed timestamps raise | Stable `INVALID_TIMESTAMP` block with regression coverage. |
| Missing coverage | Added tests for every G0-G10, every non-INTAKE phase empty evidence path, forged ledger approvals, revision variants, terminal event counts, policy payloads, and idempotent repeats. |

## Concerns

None. The implementation remains deliberately scoped to Task 1. Generic G0-G10 requirements support gate-specific adapter operations; phase transitions use the documented phase-to-gate mapping.

## Fix Round 2: Transition-Aware Approval Gates

The re-review identified that target-state-only evidence selection left the repair and recovery operations on the wrong gate. Requirements are now selected by `(current_state, next_state)` where an edge needs a different approval from the target phase's normal entry requirement:

- `GRILL -> SPEC` remains G1.
- `DIAGNOSE -> SPEC` and `DIAGNOSE -> ARCHITECTURE_REVIEW` require G6 and a `failure-bundle` bound to the canonical diagnosis/repair input hash.
- `SUBMIT -> IPIPE` remains G7.
- `ENVIRONMENT_BLOCKED -> IPIPE` requires G8 for the failed-stage rerun/manual continuation.
- `route_failure()` remains independent of the evidence gate, so failure routes into `DIAGNOSE` continue automatically.

### Round 2 TDD Record

RED command:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Initial result: 28 tests ran; 3 failed. The failures showed that a diagnosis exit selected the normal G1/target artifacts instead of G6, `DIAGNOSE -> ARCHITECTURE_REVIEW` was ungated, and `ENVIRONMENT_BLOCKED -> IPIPE` selected G7 rather than G8.

GREEN command:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Result: 29 tests passed in 0.357 seconds.

Full verification:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result: 50 tests passed in 0.325 seconds.

### Round 2 Regression Coverage

- Missing and wrong G6 approvals block `DIAGNOSE -> SPEC` without an event write; a ledger-approved G6 record advances it.
- `DIAGNOSE -> ARCHITECTURE_REVIEW` requires G6.
- A ledger-approved G1 record still advances `GRILL -> SPEC`.
- A ledger-approved G7 record advances `SUBMIT -> IPIPE`; an equally hash-bound G7 record is rejected for `ENVIRONMENT_BLOCKED -> IPIPE`, which advances only with G8.
- Automatic `REVIEW -> DIAGNOSE` failure routing remains tested without approval evidence.
- Empty evidence coverage includes `ARCHITECTURE_REVIEW` and `ENVIRONMENT_BLOCKED`.

## Fix Round 3: Source-Bound Advance Idempotency

The re-review identified that advance idempotency used only run ID, target state, and input hash. That could return an earlier `GRILL -> SPEC` result for a later `DIAGNOSE -> SPEC` operation with the same hash, skipping the G6 repair gate.

`advance()` now reads persisted run state before looking up an idempotency result. It persists an `operation_identity` on each successful advance with:

- `source_state`
- `target_state`
- `input_hash`
- `approval_gate`

The idempotency key is the SHA-256 hash of that canonical identity plus the run ID. A retry is recognized only when the last committed event is the requested target and its persisted operation identity matches the requested target/hash. Its source state and gate come from the committed event, never caller input. A different current source state creates a distinct identity and must pass that source transition's evidence gate.

### Round 3 TDD Record

RED command:

```text
python3 -m unittest scripts.tests.test_orchestrator -v
```

Initial result: 16 tests ran; one failed and one errored. A `DIAGNOSE -> SPEC` request with the same target/hash as an earlier `GRILL -> SPEC` returned the prior success instead of blocking for G6, and successful event payloads lacked the operation identity needed to recognize a real retry.

GREEN command:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Result: 31 tests passed in 0.276 seconds.

Full verification:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result: 52 tests passed in 0.267 seconds.

### Round 3 Regression Coverage

- A true `GRILL -> SPEC` retry returns the original result without adding an event and records G1 in the persisted operation identity.
- A later `DIAGNOSE -> SPEC` request with the same target and canonical input hash does not reuse the earlier G1 result, blocks without a G6 approval or event write, and then advances with an approved G6 record.

## Fix Round 4: Source-Checkpoint-Bound Advance Idempotency

The round-3 re-review identified that source state, target state, canonical input hash, and transition gate still collide after a run legally cycles back into the same source state. Advance operation identity is now also bound to `source_event_id`, derived from the latest persisted run event rather than caller evidence.

For a new advance, the idempotency key now covers:

- `source_event_id`
- `source_state`
- `target_state`
- `input_hash`
- `approval_gate`

The immediate-retry path still reads the identity from the last committed target event and returns its original receipt. After any intervening transition, a re-entered source state has a different persisted event ID, so historical success cannot bypass transition-specific approval and evidence checks. Caller-supplied `source_event_id` values do not participate in identity construction.

### Round 4 TDD Record

RED command:

```text
python3 -m unittest scripts.tests.test_orchestrator.OrchestratorTests.test_advance_retry_uses_the_last_committed_operation_identity scripts.tests.test_orchestrator.OrchestratorTests.test_reentered_source_checkpoint_does_not_reuse_prior_advance_result -v
```

Initial result: 2 tests ran and both failed. The existing identity lacked `source_event_id`; after a legal cycle back to `GRILL`, an unapproved `GRILL -> SPEC` request with the original hash returned the historical `OK` receipt instead of `APPROVAL_REQUIRED`.

GREEN command:

```text
python3 -m unittest scripts.tests.test_orchestrator.OrchestratorTests.test_advance_retry_uses_the_last_committed_operation_identity scripts.tests.test_orchestrator.OrchestratorTests.test_reentered_source_checkpoint_does_not_reuse_prior_advance_result -v
```

Result: 2 tests passed.

Focused verification:

```text
python3 -m unittest scripts.tests.test_gates scripts.tests.test_orchestrator -v
```

Result: 32 tests passed in 0.298 seconds.

Full verification:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result: 53 tests passed in 0.292 seconds.

### Round 4 Regression Coverage

- The regression uses public orchestrator operations to traverse `GRILL -> SPEC -> TASKS -> WORKSPACE -> PLAN -> IMPLEMENT -> REVIEW -> DIAGNOSE -> ARCHITECTURE_REVIEW -> GRILL`.
- Reusing the original target and hash without a current approval is blocked without an event write after re-entry.
- A caller-supplied old source event ID is ignored; the subsequent approved operation records the newly persisted `GRILL` event ID and returns a distinct receipt.
- A true immediate retry continues to return the original receipt without adding an event.

### Round 4 Concerns

None. The fix is limited to Task 1 orchestrator implementation, tests, and this report. No BGW/XFlow build or test command was run.
