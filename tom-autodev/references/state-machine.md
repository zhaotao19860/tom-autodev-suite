# State Machine

## States

```text
INTAKE -> GRILL -> SPEC -> TASKS -> WORKSPACE -> PLAN -> IMPLEMENT
       -> REVIEW -> SUBMIT -> IPIPE -> RELEASE -> RELEASE_SUCCESS
```

Each arrow requires a persisted event and `EvidenceGate` result. Approval states suspend the run without losing locks or artifact references.

`INTAKE` accepts a card whose `acceptance` list is empty, so a run may start from a plain requirement card. `GRILL` then collects the missing criteria into `acceptance_delta`. The snapshot is never rewritten, so the G0 approval stays bound to the original card hash; the union of snapshot `acceptance` and `acceptance_delta` is what Spec traceability must cover. The threshold therefore sits at the exit of `GRILL`, not at the entry of `INTAKE`.

`WORKSPACE` may also go back to `TASKS`. The WorkspaceGate binds exactly one business repository per task, so a node that spans two repositories is only provably wrong at binding time; without this edge the sole remedy would be discarding a run that already holds approved phases. Re-entry is not a rewind: it needs the G2-approved spec as evidence, the new DAG must win its own G3, and the discarded DAG stays in the ledger next to the approval it no longer justifies.

`DIAGNOSE` may go back to `IPIPE`, but only through `ipipe-rebuild`. When the diagnosed pipeline failed and the platform has expired that build's compile product (HTTP 403/404), no stage of that build can ever pass again; the same CR revisions are built once more under a G8 bound to the original build, the attempt number and the reason. The edge is a controlled move, not a repair route: no code, Spec or Plan changes, the frozen pipeline plan is carried over unchanged, the pending DIAGNOSE job is marked `SUPERSEDED` (kept, never deleted), and the old build stays on record. The evidence key for the edge is `ipipe-rebuild` plus G8 (`workflow_spec.TRANSITION_REQUIREMENTS`). A run already in `IPIPE` whose rebuilt product is gone too takes the same command and records an `IPIPE` audit event again.

`SUBMIT` may also go to `DIAGNOSE`. The CR is where the second opinions arrive: the platform's own review (小码哥) is taken after SUBMIT by design, and a human reviewer comments on the same CR. With `IPIPE` as the only exit, a confirmed defect at that point left the choice between building code somebody had just said was wrong and discarding a run holding seven approved phases. The edge goes to `DIAGNOSE`, not back to `IMPLEMENT`, because a finding is root-caused before anything is changed; the repair returns through `SPEC` or `PLAN`, or -- for an approved `CODE_ONLY` repair whose task still has an accepted Plan descending from the current TASKS DAG -- directly to `IMPLEMENT`. That `DIAGNOSE -> IMPLEMENT` entry pins the accepted Plan (`plan_artifact_id`/`plan_content_hash`) and takes `baseline_revisions` from the Plan's repositories, never from the diagnosis' frozen candidate revisions; without such a Plan it re-enters `PLAN`. The repair lands as a new revision on the same CR. A repaired task owes iCode that new change set — the submission frontier joins on the task's *current* change set, so the earlier receipt cannot answer for the repair.

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
| `ENVIRONMENT_REPAIR_FORBIDDEN` | A diagnosis routed `REPAIR`, `SPEC` or `GRILL` while the failure has an authoritative environment classification (strong evidence only, see failure-taxonomy.md: structured platform fields, a control-plane-proven dependency fetch failure, or `ENV_*`/`PIPELINE_TRANSIENT`/`JOB_PARAMETER_OVERRIDE_UNSUPPORTED`/`COMPILE_ONLY_TRIGGER_UNSUPPORTED`; keyword hints are advisory). Relabelling it in the diagnosis does not help. The draft is refused before G6 and a completion stops with this reason. `IPIPE` BLOCKED evidence goes to `ENVIRONMENT_BLOCKED` (recover with G8 back to `IPIPE`); `DIAGNOSE -> ENVIRONMENT_BLOCKED` is not an edge of the current workflow spec, so an environment-class diagnosis still ends in `STOPPED`. |
| `ENV_TRANSIENT` | Run at most three read-only health rechecks. |
| `REVISION_MISMATCH` | Stop; never reuse the pipeline result. |
| `APPROVAL_REJECTED` / `APPROVAL_TIMEOUT` | Persist handoff and stop. On G1–G6 a “需修改”/historical REJECT with feedback is redone in place; the next `继续` re-enters the producer and requests a new hash-bound gate. |
| `USER_REQUIREMENT_CHANGED` after G1 | PLAN reports `report-producer-conflict`, IMPLEMENT reports `report-producer-block`; the diagnosis must keep that classification and route `GRILL` (or `STOPPED`), and G6 sends the run to `GRILL` so the decision log itself is amended. SPEC/PLAN cannot absorb it: they derive from the decision log and would restore the old rule. |
| Earlier phase must change | Approved `CHANGE_REQUEST` (`rewind`) re-enters `REWIND_TARGETS[state]`; ARCHITECTURE_REVIEW leaves only this way. |

## Recovery

Write the intent event before every external action and the result event after it. Use `requirement_id + task_id + spec_version + action + input_hash` as the idempotency key. Resume from the last confirmed result event and never infer success from a pending intent.
