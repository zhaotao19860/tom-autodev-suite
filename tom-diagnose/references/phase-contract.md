# Diagnose Phase Contract

Use the suite [producer contract](../../tom-autodev/references/producer-contract.md) and current [diagnosis schema](../../tom-autodev/schemas/diagnosis.schema.json). Return content only, not an ArtifactEnvelope. This contract does not apply to a standalone remote investigation report.

| Fields | Evidence source |
|---|---|
| task_id, attempt, frozen_revisions, build_id/stage_id/job_id, environment_fingerprint | Parent's frozen failure bundle and durable repair history; no invented pipeline IDs for a source Review failure |
| failure_signature | Current failure artifact/event supplied by the controller; a newly inferred cause belongs in hypothesis/comparison, not a replacement identity |
| log_evidence, reproduction, comparison | Named immutable logs/receipts, observed result and comparable passing baseline; distinguish an observation from a proposed experiment |
| classification, hypothesis, minimal_verification | Cause category, one causal explanation and the evidence that confirms/refutes it; put confidence/limitations in these supported text fields rather than adding unknown keys |
| route, repair_direction, repair_plan, repair_scope | Confirmed route proposal; CODE_ONLY keeps behavior/Spec, SPEC_AMENDMENT changes behavior, TASK_SCOPE changes task boundaries |
| repair_diff_hash | `null` when no patch exists, including route REPAIR; otherwise the hash of an actual already-existing diff. Diagnosis itself does not create a patch |

For insufficient evidence, `route=DIAGNOSIS_INCOMPLETE`, `evidence_state=INSUFFICIENT`, `hypothesis=null`, `repair_direction=null`, `repair_diff_hash=null`, and `repair_plan=[]`. Required evidence arrays must explain the actual missing/observed material, not claim a test ran.

For a confirmed repair proposal, use `route=REPAIR`, `evidence_state=SUFFICIENT`, a verified hypothesis and a concrete repair plan, with `repair_diff_hash=null` until a patch exists. Both the JSON Schema and semantic validator accept this proposal. G6 approves the diagnosis and direction; PLAN/IMPLEMENT produces the candidate and a fresh G5 binds the actual change. Do not fabricate a hash, edit code during DIAGNOSE, downgrade sufficient evidence, or change the route to bypass validation. Non-REPAIR routes keep `repair_diff_hash=null`.

For a source Review failure, set `build_id`, `stage_id`, `job_id` and `environment_fingerprint` to `null`. The controller binds the diagnosis to the predecessor artifact, task and frozen revisions; reference the actual Review findings/source evidence in the supported evidence fields. Do not borrow identifiers from an earlier pipeline. If the source supplies no failure signature, derive a stable signature from the actual finding/cause, without build or attempt noise.

For an iPipe failure, copy its exact `build_id`, `environment_fingerprint` and `failure_signature`. Select an observed `stage_id` and a `job_id` that belongs to that stage and appears in the failure bundle. A null stage/job is allowed only when the source has no corresponding stage/jobs; absence is not permission to invent one. The controller rejects erased or unrelated execution identities before locking the draft.

When the user changed a requirement after G1 (the entry block or conflict says `USER_REQUIREMENT_CHANGED`, or the evidence shows it), keep `classification=USER_REQUIREMENT_CHANGED` and use `route=GRILL` (or `STOPPED`). Put the old and new rule in `comparison` and the decision-log change in `repair_direction`. The controller rejects any other route for that class, a relabelled class, and `route=GRILL` for anything else; on approval the direction and `repair_plan` reach GRILL as `revision_context`.

The parent settles the applicable G6 and validates the route. The producer neither calls `repair_policy` with guessed history nor marks a FailureCase resolved; a verified repair result and its scope are required.

For an environment-class failure (`ENV_UNSATISFIED`, `ENV_TRANSIENT`, `PIPELINE_TRANSIENT`, `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`, ...) whose cause was resolved outside the code and whose failed stage was already re-run under G8 after the failure, use `route=IPIPE`: no business patch, the run returns to the pipeline and continues with the remaining modules. The controller refuses `route=IPIPE` for a non-environment class, and stops the run (`IPIPE_RESUME_WITHOUT_RERUN`) when no confirmed G8 rerun of `stage_id` exists after the failure.

