# Failure Taxonomy

| Reason | Repair allowed | Route |
|---|---:|---|
| `LOCAL_EXECUTION_FORBIDDEN` | no | Refuse local project build/test and create an iPipe validation plan. |
| `REVIEW_FAILED` | after diagnosis | Confirm finding, then diagnose. |
| `REVIEW_INCOMPLETE` | no | Use human Review fallback. |
| `CODE_FAILURE` | after diagnosis | Diagnose, revise Spec/Task Plan, generate patch. |
| `TEST_FAILURE` | after diagnosis | Distinguish product, test, and environment defects. |
| `BASELINE_UNVERIFIED` | no | Require human baseline decision. |
| `DIAGNOSIS_INCOMPLETE` | no | Gather evidence or request an approved iPipe diagnostic stage. |
| `ENV_UNSATISFIED` | no | Restore environment; keep business code unchanged. |
| `ENV_TRANSIENT` | no | Perform read-only health rechecks. |
| `PIPELINE_TRANSIENT` | no code repair | Retry read-only queries; G8 controls a stage rerun. |
| `REVISION_MISMATCH` | no | Stop and discard the result. |

Use one root-cause hypothesis per repair round. Stop automatic proposals after two rounds with the same signature and no progress. Force an architecture Review after three unsuccessful approved fixes. Stop after five repair rounds for a task.
