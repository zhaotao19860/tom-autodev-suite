# State Machine

## States

```text
INTAKE -> GRILL -> SPEC -> TASKS -> WORKSPACE -> PLAN -> IMPLEMENT
       -> REVIEW -> SUBMIT -> IPIPE -> RELEASE -> RELEASE_SUCCESS
```

Each arrow requires a persisted event and `EvidenceGate` result. Approval states suspend the run without losing locks or artifact references.

`INTAKE` accepts a card whose `acceptance` list is empty, so a run may start from a plain requirement card. `GRILL` then collects the missing criteria into `acceptance_delta`. The snapshot is never rewritten, so the G0 approval stays bound to the original card hash; the union of snapshot `acceptance` and `acceptance_delta` is what Spec traceability must cover. The threshold therefore sits at the exit of `GRILL`, not at the entry of `INTAKE`.

`WORKSPACE` may also go back to `TASKS`. The WorkspaceGate binds exactly one business repository per task, so a node that spans two repositories is only provably wrong at binding time; without this edge the sole remedy would be discarding a run that already holds approved phases. Re-entry is not a rewind: it needs the G2-approved spec as evidence, the new DAG must win its own G3, and the discarded DAG stays in the ledger next to the approval it no longer justifies.

`SUBMIT` may also go to `DIAGNOSE`. The CR is where the second opinions arrive: the platform's own review (小码哥) is taken after SUBMIT by design, and a human reviewer comments on the same CR. With `IPIPE` as the only exit, a confirmed defect at that point left the choice between building code somebody had just said was wrong and discarding a run holding seven approved phases. The edge goes to `DIAGNOSE`, not back to `IMPLEMENT`, because a finding is root-caused before anything is changed; the repair returns through `PLAN`/`SPEC` and lands as a new revision on the same CR. A repaired task owes iCode that new change set — the submission frontier joins on the task's *current* change set, so the earlier receipt cannot answer for the repair.

 — it carries the hash of what was written to it — so the second attempt at a phase cannot publish over the first one's title. `_phase_title` appends the attempt (`03-tasks-r2`), counted per phase *and* task so the second task's first plan is not mistaken for the first task's second attempt. Both documents stay in the run index, which is what makes the discarded artifact auditable. A title KU refuses (`KU_IMMUTABLE_CONFLICT`) writes nothing, so `KnowledgeSync` closes that operation's intent instead of leaving the run parked in `RECOVERY_REQUIRED`; every other failure, including any unknown result, stays open for recovery to query.

KU holds an edit separately from what a query returns: `edit-content` and `edit-mdsl-content` land in the edit state, `query-content` answers from the preview, and only `publish-doc` moves one to the other. So a publish receipt records that one preview text was published, not that the edit state is empty — `_publish` keyed only on the preview hash would short-circuit on that receipt and strand every edit made since, which wedges the run while each retry adds another copy to the edit state. When the receipt exists but verification fails, the publish is repeated (`ku.document.publish.reflush`, journalled under the observed version so a crash re-uses the same intent) and the entry is verified against the fresh preview.

## Failure Transitions

| Reason | Next state |
|---|---|
| `ACCEPTANCE_CRITERIA_MISSING` | Return to `GRILL`; Spec cannot start without at least one criterion. |
| `ACCEPTANCE_REQUIRED` | Stay in `GRILL`; `tom-grill` stopped before emitting an artifact because the union would stay empty. |
| `ACCEPTANCE_DELTA_CONFLICT` | Return to `GRILL`; an `acceptance_delta` id already exists in the snapshot. |
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
