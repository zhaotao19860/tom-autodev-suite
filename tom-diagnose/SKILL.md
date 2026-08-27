---
name: tom-diagnose
description: Use when Review, iPipe, release, code, or product-test evidence reports a failure that may require a repair or environment decision.
---

# Tom Diagnose

## Inputs and Output

Consume a Failure Evidence Bundle containing stage/job, complete logs, failing and passing revisions, diff, Spec/Task Plan, Review findings, baseline, environment fingerprint, knowledge/impact references, and previous repair history. Produce a `Diagnosis` with reproduction status, comparison evidence, failure classification, one root-cause hypothesis, minimal verification, confidence, and recommended route.

Follow [`references/phase-contract.md`](references/phase-contract.md). Emit the `diagnosis` ArtifactEnvelope to the parent for KU persistence, iCafe linking, and G6 approval; do not call iPipe directly.

## Procedure

1. Freeze the failing revision, test revision, baseline, pipeline build, stage/job, and environment fingerprint.
2. Confirm whether the failure is reproducible from existing iPipe evidence; do not claim a local build/test as reproduction.
3. Compare the failing revision with the latest passing revision using the same stage, inputs, toolchain, and environment evidence.
4. Separate code/test behavior from `ENV_UNSATISFIED`, `ENV_TRANSIENT`, `PIPELINE_TRANSIENT`, `REVISION_MISMATCH`, and release-platform failures.
5. Use repository impact and knowledge evidence to select one causal hypothesis. State what evidence would disprove it.
6. Verify only the minimal hypothesis using existing evidence or an explicitly approved iPipe diagnostic stage. If root cause or verification is missing, return `DIAGNOSIS_INCOMPLETE`.
7. For a confirmed product/test cause, return a minimal Spec repair proposal and updated Task Plan request. Do not edit code in this phase.

## Repair Budget

Pass diagnosis history to `repair_policy.next_action`. Environment and baseline failures never return a business repair. Two identical no-progress signatures stop automatic proposals; three unsuccessful approved fixes force architecture Review; five repair rounds stop the task.

## Gate

Human G6 approval must cover the Diagnosis, repair direction, updated Task Plan, and later repair diff. Review findings must first be `CONFIRMED`; `NEEDS_CLARIFICATION` pauses the run. Never guess from a log tail, turn an environment error into a code patch, run project tests on Mac, or call iCode/iPipe directly.

**REQUIRED PARENT:** Return the Diagnosis or explicit stop reason to `tom-autodev`.
