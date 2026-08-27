# State Machine

## States

```text
INTAKE -> GRILL -> SPEC -> TASKS -> WORKSPACE -> PLAN -> IMPLEMENT
       -> REVIEW -> SUBMIT -> IPIPE -> RELEASE -> RELEASE_SUCCESS
```

Each arrow requires a persisted event and `EvidenceGate` result. Approval states suspend the run without losing locks or artifact references.

## Failure Transitions

| Reason | Next state |
|---|---|
| `REQUIREMENT_CHANGED` | Invalidate downstream approvals and return to `GRILL`. |
| `BASELINE_UNVERIFIED` | Stop for human baseline decision; high-risk changes remain stopped. |
| `REVIEW_FAILED` | Validate the finding, then enter `DIAGNOSE`. |
| `CODE_FAILURE` / `TEST_FAILURE` | Build a Failure Evidence Bundle and enter `DIAGNOSE`. |
| `DIAGNOSIS_INCOMPLETE` | Stop for evidence or an approved iPipe diagnostic stage. |
| `ENV_UNSATISFIED` | Stop code repair and wait for environment recovery. |
| `ENV_TRANSIENT` | Run at most three read-only health rechecks. |
| `REVISION_MISMATCH` | Stop; never reuse the pipeline result. |
| `APPROVAL_REJECTED` / `APPROVAL_TIMEOUT` | Persist handoff and stop. |

## Recovery

Write the intent event before every external action and the result event after it. Use `requirement_id + task_id + spec_version + action + input_hash` as the idempotency key. Resume from the last confirmed result event and never infer success from a pending intent.
