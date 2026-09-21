# Diagnose Phase Contract

Use the suite [producer contract](../../tom-autodev/references/producer-contract.md) and current [diagnosis schema](../../tom-autodev/schemas/diagnosis.schema.json). Return content only, not an ArtifactEnvelope. This contract does not apply to a standalone remote investigation report.

| Fields | Evidence source |
|---|---|
| task_id, attempt, frozen_revisions, build_id/stage_id/job_id, environment_fingerprint | Parent's frozen failure bundle and durable repair history; no invented pipeline IDs for a source Review failure |
| failure_signature | Current failure artifact/event supplied by the controller; a newly inferred cause belongs in hypothesis/comparison, not a replacement identity |
| log_evidence, reproduction, comparison | Named immutable logs/receipts, observed result and comparable passing baseline; distinguish an observation from a proposed experiment |
| classification, hypothesis, minimal_verification | Cause category, one causal explanation and the evidence that confirms/refutes it; put confidence/limitations in these supported text fields rather than adding unknown keys |
| route, repair_direction, repair_plan, repair_scope | Confirmed route proposal; CODE_ONLY keeps behavior/Spec, SPEC_AMENDMENT changes behavior, TASK_SCOPE changes task boundaries |
| repair_diff_hash | Hash of an actual already-existing diff only; diagnosis itself does not create a patch |

For insufficient evidence, `route=DIAGNOSIS_INCOMPLETE`, `evidence_state=INSUFFICIENT`, `hypothesis=null`, `repair_direction=null`, `repair_diff_hash=null`, and `repair_plan=[]`. Required evidence arrays must explain the actual missing/observed material, not claim a test ran.

For a confirmed repair proposal, check both the current JSON Schema and the controller's semantic validator. At consolidation time the schema allows a null repair hash but `_validate_diagnosis` requires a real hash for route REPAIR. Without an existing diff, report this contract mismatch to the parent; do not fabricate a hash, edit code during DIAGNOSE, mark sufficient evidence insufficient, or change the route just to bypass validation. Re-evaluate against the current controller when that contract is fixed.

For Review failures without pipeline identifiers, use a documented parent mapping if supplied. If none exists, return the missing identity contract to the parent instead of making up build/stage/job IDs.

The parent settles the applicable G6 and validates the route. The producer neither calls `repair_policy` with guessed history nor marks a FailureCase resolved; a verified repair result and its scope are required.
