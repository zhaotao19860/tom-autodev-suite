# Task 1 Re-Review: Fix Round 1

## Verdict

- Spec: **FAIL**
- Quality: **FAIL**

## Review Basis

- Reviewed the original Task 1 brief, original review, and implementation report, then statically inspected the current Task 1 control-plane files and their tests.
- No tests were rerun. The implementation report records 25 focused tests and 46 full-suite tests passing; this re-review does not independently attest to those results.
- No BGW/XFlow build or test command was run.

## Original Finding Disposition

| Original finding | Disposition | Evidence |
|---|---|---|
| Critical: Gate approval can be forged and G6/G8/G10 are not modeled | **NOT ADDRESSED** | The ledger-backed lookup prevents caller-forged records for mapped phase actions (`scripts/orchestrator.py:164` through `scripts/orchestrator.py:173`) and generic G0-G10 entries now exist (`scripts/evidence_policy.py:33` through `scripts/evidence_policy.py:35`). However, `DIAGNOSE` and `ENVIRONMENT_BLOCKED` still have no phase requirements, so `REVIEW -> DIAGNOSE` can pass with caller-supplied matching hashes and no G6 approval. Also, the recovery edge `ENVIRONMENT_BLOCKED -> IPIPE` is checked as G7 because requirements depend only on `next_state` (`scripts/evidence_policy.py:25` through `scripts/evidence_policy.py:26`), rather than requiring G8. This leaves mandatory G6/G8 operations unbound to ledger approval. |
| Critical: Empty revision evidence satisfies a release gate | **ADDRESSED** | Release evidence now requires non-empty, typed revision mappings and optional required repositories (`scripts/evidence_policy.py:56` through `scripts/evidence_policy.py:68`, `scripts/evidence_policy.py:92` through `scripts/evidence_policy.py:99`); empty, `None`, partial, and mismatch coverage was added (`scripts/tests/test_gates.py:208` through `scripts/tests/test_gates.py:242`). |
| Important: `stop()` bypasses policy and writes after a terminal state | **ADDRESSED** | Terminal states are explicit and `stop()` uses `validate_stop()` before any write (`scripts/transition_policy.py:21` through `scripts/transition_policy.py:26`, `scripts/orchestrator.py:150` through `scripts/orchestrator.py:162`). Tests assert terminal operations do not append events (`scripts/tests/test_orchestrator.py:140` through `scripts/tests/test_orchestrator.py:159`). |
| Important: Successful events omit the policy decision | **ADDRESSED** | Advance, failure-route, and stop payloads now contain `policy_decision` (`scripts/orchestrator.py:93` through `scripts/orchestrator.py:101`, `scripts/orchestrator.py:136` through `scripts/orchestrator.py:140`, `scripts/orchestrator.py:157` through `scripts/orchestrator.py:161`). |
| Important: Malformed timestamps raise instead of blocking structurally | **ADDRESSED** | Timestamp parsing returns `None` for invalid input and the gate produces `INVALID_TIMESTAMP` (`scripts/evidence_gate.py:26` through `scripts/evidence_gate.py:36`, `scripts/evidence_gate.py:55` through `scripts/evidence_gate.py:62`), with a regression test (`scripts/tests/test_gates.py:244` through `scripts/tests/test_gates.py:263`). |
| Important: Required transition and evidence coverage is absent | **NOT ADDRESSED** | Coverage improved materially, but it still omits both legal non-INTAKE states `ARCHITECTURE_REVIEW` and `ENVIRONMENT_BLOCKED` from the empty-evidence test (`scripts/tests/test_gates.py:53` through `scripts/tests/test_gates.py:74`) and has no test proving G6 is required for direct diagnosis or G8 is required for recovery to iPipe. Those omissions mask the remaining Critical approval-routing defect. |

## Remaining Findings

### Critical: Direct diagnosis and iPipe recovery bypass their designated approval gates

- Axis: Spec
- Blocking: yes
- Evidence: `scripts/transition_policy.py:14` through `scripts/transition_policy.py:20` allow `REVIEW -> DIAGNOSE` and `ENVIRONMENT_BLOCKED -> IPIPE`. `scripts/evidence_policy.py:17` through `scripts/evidence_policy.py:35` define neither a `DIAGNOSE` requirement using G6 nor a transition-sensitive recovery requirement using G8. Consequently, `scripts/orchestrator.py:88` through `scripts/orchestrator.py:91` accepts a direct diagnosis transition with only caller-provided `input_hash`/`approved_input_hash`; recovery to IPIPE uses G7 from `scripts/evidence_policy.py:26`.
- Affected acceptance criteria: every G0-G10 approval binds the exact canonical input hash; missing gate approval is rejected; phase evidence uses fixed requirements.
- Required correction: represent approval/evidence requirements by `(current_state, next_state)` or equivalent operation, bind diagnosis work to G6 and recovery/rerun to G8, then add no-write tests for missing/wrong approval on both paths.

## New Findings

No new Critical or Important breakage was introduced by the fix round. The remaining Critical finding is an incomplete correction of the original approval-gate finding.
