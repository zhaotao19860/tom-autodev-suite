# Task 1: Close Evidence and Transition Gates

## Files

- Create `scripts/evidence_policy.py` and `scripts/transition_policy.py`.
- Modify `scripts/evidence_gate.py` and `scripts/orchestrator.py`.
- Test with `scripts/tests/test_gates.py` and `scripts/tests/test_orchestrator.py`.

## Required behavior

- Empty evidence cannot advance a non-INTAKE phase.
- Missing input/approved hashes, gate approval, revision evidence, or required environment evidence is rejected.
- G0-G10 approvals bind the exact canonical input hash.
- `advance()` and `route_failure()` must use the same legal-transition policy.
- Illegal and terminal-state transitions return structured blocks without writing state events.
- Idempotency is preserved and policy evidence is recorded.
- Local BGW/XFlow build and test execution is forbidden.

## Verification

Run `python3 -m unittest discover -s scripts/tests -v` from the Skill root.
