# Task 9 Fix Round 1 Scoped Re-review

## Verdicts

- **Specification: FAIL** - I2 and I3 are addressed, but I1 remains incomplete and round 1 introduced N1.
- **Standards/content quality: FAIL** - the NPL and Review wording is now consistent, but the parent gate contract remains internally contradictory.
- **Overall: FAIL** - one original Important finding remains and one new Important contradiction must be repaired.

## Original Findings

### I1 - NOT ADDRESSED

The controller table now covers `WORKSPACE`, `SUBMIT`, `IPIPE`, `RELEASE`, and terminal outcomes, and names G8/G9/G10. However, the scoped requirement asked for explicit inputs, receipts, completion predicates, approval-hash ownership, and stops for G8/G9/G10:

- `tom-autodev/references/phase-protocol.md:34` describes the normal `ipipe-evidence` artifact but does not define the durable G8 rerun/manual-continuation receipt, its exact completion predicate, or replay/unknown-result stop behavior.
- `tom-autodev/references/phase-protocol.md:38-40` leaves G10 as prose. It names the `RunSummary`, candidate hash, KU/iCafe persistence, rollback, and forbidden targets, but does not define a G10 proposal/result receipt, a success predicate, or approval timeout/rejection and failed-validation terminal results in the controller contract.
- The new `WORKSPACE` contract creates the gate contradiction recorded as N1 below.

G9 is sufficiently described for this scoped review. I1 remains open until G8 and G10 have the same explicit contract shape as the controller rows and the G4 contradiction is removed.

### I2 - ADDRESSED

`tom-lang-npl/references/error-patterns.md:3-14` now makes the canonical parent class normative for every row and keeps NPL detail in a subtype. It distinguishes a healthy-runner chip/source limit (`CODE_FAILURE`) from missing runner/service/hardware capacity (`ENV_UNSATISFIED`) and temporary eligibility failure (`ENV_TRANSIENT`), and maps pipeline interruption and identity drift to `PIPELINE_TRANSIENT` and `REVISION_MISMATCH`. Routes now agree with the parent taxonomy and preserve the Mac/iPipe boundary.

### I3 - ADDRESSED

`tom-review/SKILL.md:33-37`, `tom-review/references/phase-contract.md:3`, `tom-autodev/SKILL.md:37`, and `tom-autodev/references/phase-protocol.md:21` consistently state that Review produces evidence but carries no approval. Dual-axis PASS is only a prerequisite for a separately parent-owned, exact-Change-Set-hash-bound G7 approval. Provider `INCOMPLETE`, `NEEDS_CLARIFICATION`, missing inputs, baseline/input hash drift, and blocking findings now have explicit stop or diagnosis routes.

## New Important Findings

### N1 - WORKSPACE assigns G4 to the wrong approval object

`tom-autodev/references/phase-protocol.md:32` says the parent owns G4 in `WORKSPACE` and binds it to the Task DAG/task identity and WorkspaceGate input hash. That conflicts with `tom-autodev/references/approval-policy.md:9`, `tom-plan/SKILL.md:28-30`, and the child phase table at `phase-protocol.md:19`, all of which define G4 as approval of the completed per-task `Task Plan`. A pre-Plan WorkspaceGate cannot be approved using the hash of a Task Plan that does not yet exist, and two different approval objects cannot share one canonical G4 meaning.

Required repair: make WorkspaceGate a fail-closed evidence prerequisite without reassigning G4, or introduce an already-defined distinct gate; retain G4 exclusively for the exact Task Plan hash across all documents.

## New Critical Findings

None.

## Static Checks

- All five round-1 SHA-256 values match the current files.
- No round-1 scoped file contains Codex or standalone `tom-autorelease` wording.
- No new wording permits local BGW/XFlow compile, test, regression, integration, Docker, NCS, simulator, or release execution.
- No Skill was edited, no live service was called, and no project command was run during this review.
