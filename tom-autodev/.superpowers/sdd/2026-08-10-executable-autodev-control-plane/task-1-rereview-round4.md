# Task 1 Re-Review: Fix Round 4

## Verdict

- Spec: **PASS**
- Quality: **PASS**

## Verification

- **Same-state re-entry is collision-safe.** New advance identities obtain `source_event_id` from `current["events"][-1]`, not from `evidence_context` (`scripts/orchestrator.py:88` through `scripts/orchestrator.py:92`). The identity and key include that checkpoint alongside source state, target state, input hash, and selected gate (`scripts/orchestrator.py:190` through `scripts/orchestrator.py:199`, `scripts/orchestrator.py:239` through `scripts/orchestrator.py:243`). A later re-entry to the same phase therefore produces a distinct idempotency key.
- **True immediate retries are preserved.** The retry path accepts only a complete persisted identity on the latest event whose state matches the requested target, then looks up that precise receipt (`scripts/orchestrator.py:80` through `scripts/orchestrator.py:86`, `scripts/orchestrator.py:201` through `scripts/orchestrator.py:237`). It uses no caller-supplied identity data.
- **Caller identity is ignored.** The regression supplies the first `GRILL` event ID after a public-operation cycle re-enters `GRILL`; the unapproved call is blocked, and the subsequent approved call records the re-entered event ID rather than the caller value (`scripts/tests/test_orchestrator.py:172` through `scripts/tests/test_orchestrator.py:280`).
- **Regression adequacy.** The retry test still asserts one committed transition and the original receipt, including its source event ID (`scripts/tests/test_orchestrator.py:140` through `scripts/tests/test_orchestrator.py:170`). The re-entry test traverses the legal phase loop through public orchestrator operations before asserting no stale receipt is reused.

## Findings

No remaining or newly introduced Critical or Important findings in Task 1 scope.

## Review Basis

This was a static re-review. Tests were not rerun; the latest implementation report records 32 focused and 53 full-suite tests passing, which was not independently verified. No BGW/XFlow build or test command was run.
