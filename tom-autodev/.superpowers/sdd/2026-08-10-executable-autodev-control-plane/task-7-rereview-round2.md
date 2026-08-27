# Task 7 Fix Round 2 Re-review

Date: 2026-08-11

## Review Basis

- Scope: static re-review of the Task 7 round-2 repair against `task-7-brief.md`, its Repair Rulings, `task-7-rereview-round1.md`, and the exact production/test files listed in the round-2 implementation report.
- Method: source/schema/test inspection and SHA-256 checks only. No test, compile, project, adapter, iCode, iPipe, KU, iCafe, or other live command was run, and no implementation file was edited.
- The supplied `222/222` focused result, `380/380` full-suite result, compileall exit 0, JSON checks, and clean safety scans are evidence only.
- Round-1 re-review SHA-256: `6f91f4832d2e2fc38cf0bd12c9c8fabc55f02e14ae67a8de63ca6cbc451424d3`.
- Current implementation report SHA-256: `992a6ccb2f45ae4c01e25ae9cb16503d1d1078b1ec434a6216d28b0ded1d95d0`.
- All 11 current round-2 production/test file hashes match the implementation-report appendix exactly.
- M1 remains the approved deferred Minor finding outside this repair loop. Run-summary production/binding remains deferred to Task 8 under Repair Ruling 5.

## Verdicts

- Specification: **FAIL**
- Standards: **FAIL**
- Overall: **FAIL**
- Gate: **FAIL**; Task 7 must not proceed to G7/iCode approval.

Specification fails on blocking `I4` and new `N2`. Standards fails on blocking `I8` and `N2`. All open and new findings are `CONFIRMED`; there are no `NEEDS_CLARIFICATION` findings and no provider-capability gap.

## Disposition Summary

| ID | Disposition |
| --- | --- |
| C1 | **ADDRESSED** |
| C2 | **ADDRESSED** |
| C3 | **ADDRESSED** |
| C4 | **ADDRESSED** |
| C5 | **ADDRESSED** |
| C6 | **ADDRESSED** |
| C7 | **ADDRESSED** |
| I1 | **ADDRESSED** |
| I2 | **ADDRESSED** |
| I3 | **ADDRESSED** |
| I4 | **NOT ADDRESSED** |
| I5 | **ADDRESSED** |
| I6 | **ADDRESSED** |
| I7 | **ADDRESSED** |
| I8 | **NOT ADDRESSED** |
| N1 | **ADDRESSED** |
| N2 | **NEW - OPEN** |

Addressed IDs: `C1`, `C2`, `C3`, `C4`, `C5`, `C6`, `C7`, `I1`, `I2`, `I3`, `I5`, `I6`, `I7`, `N1`.

Open IDs: `I4`, `I8`.

New IDs: `N2` (Important, blocking).

## Round-2 Addressed Findings

### C2 - Strict G0 snapshot and collaboration binding

**ADDRESSED** (Specification). `Orchestrator.start()` now rejects a missing or invalid snapshot before run creation, prepares the complete collaboration binding, and hashes the complete INTAKE payload at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:44` through `:113`. The shared collaboration validation requires the canonical group/session/member binding and exact durable create intent/receipt at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:20` through `:218` and `:266` through `:337`. `PhaseProtocol.next()` and INTAKE completion fail closed on malformed prerequisites or receipt mismatch at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:112` through `:183`, `:223`, and `:290` through `:333`.

### I1 - Exact historical completion replay

**ADDRESSED** (Specification and Standards). `ArtifactStore.phase_artifact()` loads an explicitly recorded artifact identity at `/Users/tom/Desktop/skills/tom-autodev/scripts/artifact_store.py:251`. Both ordinary phase replay and iPipe replay now validate the exact `artifact_id` stored in the definite result rather than redirecting through the latest same-phase artifact at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:519` and `:961`.

### I2 - Production acceptance traceability

**ADDRESSED** (Specification). One normalizer now accepts production string IDs and object IDs while rejecting empty, malformed, and duplicate normalized values at `/Users/tom/Desktop/skills/tom-autodev/scripts/requirement_snapshot.py:48` through `:90`. Snapshot validation and named-schema validation use the same contract at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:54`, and SPEC traceability consumes those normalized IDs at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:345`.

### I3 - Baseline-to-candidate revision flow

**ADDRESSED** (Specification). IMPLEMENT actions separately bind baseline revisions while accepting generated candidate revisions in the Change Set; completion, G5, checkpoint, and REVIEW propagation use the candidate set at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:140` through `:176`, `:241`, `:280`, `:396`, `:485`, and `:1014`.

### I5 - Decision-log semantic consistency

**ADDRESSED** (Specification). All three decision-result states now have coherent status/frontier requirements, and recorded decisions require unique IDs/options with a declared choice at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:250` through `:278`.

### N1 - Stable evidence preservation

**ADDRESSED** (Specification). Final phase and iPipe envelopes use an ordered, deduplicating union of draft/source evidence and canonical publication receipts at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:459`, `:823`, `:1034`, and `:1122`; the preserved list remains available to the next action.

## Prior Addressed Findings

`C1`, `C3`, `C4`, `C5`, `C6`, `C7`, `I6`, and `I7` remain **ADDRESSED**. Static inspection of the round-2 touched paths found no regression in snapshot hashing, generated KU identity, atomic completion, DAG frontier selection, REVIEW routing, controller-descriptor rejection, ArtifactStore integrity/indexing, or stable exception handling.

## Open Findings

### I4 - Owned iPipe binding has no executable production event-formation path

**NOT ADDRESSED** (`CONFIRMED`, Specification, Important, blocking).

The new validator correctly requires profile-owned pipeline/module/release identity, revisions, environment, submission identity, and exact `controller_binding` metadata at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:895` through `:959`. However, no production controller constructs that accepted IPIPE event.

The existing public transition path, `Orchestrator.advance()`, stores caller evidence only under `payload["evidence"]` at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:219` through `:254`, while `_ipipe_binding_error()` requires those fields at the event payload top level. `IcodeRuntime` returns module/revision receipt data but does not create the required submission artifact with `metadata.controller_binding` at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:285` through `:320`. Static search found no production caller outside tests that directly forms the required IPIPE transition. Consequently, the strengthened `next(IPIPE)` and ingestion checks reject every event obtainable through the existing orchestrator path. The iPipe artifact/checkpoint path still lacks the real production predecessor required by Repair Ruling 5.

### I8 - iPipe repair coverage still bypasses the production boundary

**NOT ADDRESSED** (`CONFIRMED`, Standards, Important, blocking).

The round adds useful negative, replay, and evidence-preservation assertions, but `ipipe_case()` manually fabricates a generic submission artifact with `controller_binding` metadata at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_phase_protocol_repair.py:831` through `:864`. The tests then call `StateStore.transition(..., "IPIPE", payload)` directly, including at lines `1007`, `1024`, `1078`, `1089`, and `1103`, rather than obtaining the event from the orchestrator/iCode/iPipe controller flow. These tests prove the isolated validator after a synthetic precondition; they do not prove that the production path can establish that precondition. This does not satisfy round-1 acceptance point 8's requirement for production-shaped orchestrator/protocol flows.

## New Fix-Round Finding

### N2 - Advertised CLI `start` can never supply the now-required snapshot

- Classification: `CONFIRMED`
- Axes: Specification and Standards
- Severity: Important
- Blocking: Yes
- Locations: `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:44`, `:57`, `:756`, `:780`, and `:790`
- Affected requirements: design control-plane command contract at `/Users/tom/Desktop/skills/tom-autodev/docs/superpowers/specs/2026-08-10-executable-autodev-control-plane-design.md:50`; Task 7 Repair Ruling 4.

The strict Python API repair is correct: `Orchestrator.start()` rejects `requirement_snapshot=None` with `ICAFE_SNAPSHOT_REQUIRED`. The CLI parser, however, accepts only `requirement_id` and `project`, and `main()` always calls `start()` without a snapshot. It has no snapshot input and no path through the established iCafe adapter to capture one. Therefore every CLI `start` invocation against an otherwise ready project returns no run. `main()` also returns exit code 0 for `ICAFE_SNAPSHOT_REQUIRED`, falsely reporting this deterministic failure as command success. No test exercises `main(["start", ...])`.

This is Important rather than Critical: it fails closed and the programmatic API remains usable, so it does not approve or persist an unsafe run. It is nevertheless blocking because the design explicitly advertises `start` as a control-plane command and the round-2 change made that public entry point universally nonfunctional.

## Required Acceptance For The Next Repair

1. A production controller path must create the submission artifact and transition `SUBMIT -> IPIPE` with the exact top-level profile/pipeline/module/release/revision/environment/submission/G7 binding consumed by `PhaseProtocol.next()` and `ingest_ipipe_evidence()`.
2. Focused tests must obtain the IPIPE event through that production controller path, then cover success, each owned-binding mismatch, nested evidence rejection, historical replay, profile drift, and evidence propagation without directly seeding state or fabricating the predecessor metadata.
3. The CLI `start` command must capture a canonical snapshot through the established iCafe adapter or accept a validated snapshot input, pass it to `Orchestrator.start()`, and return nonzero for every non-ready result. A focused CLI test must prove a valid start and missing/invalid snapshot failure.

## Axis Reports

- **Specification: FAIL.** `I4` leaves the iPipe artifact/checkpoint contract unreachable through production ownership paths, and `N2` leaves the advertised CLI start command unusable.
- **Standards: FAIL.** `I8` substitutes manually seeded internal state for the production boundary under test, and `N2` has no CLI-level regression coverage and reports a failed start with exit code 0.
- **Overall/Gate: FAIL.** Blocking confirmed findings remain; the Change Set returns to diagnosis and is not eligible for G7 approval.

## Verification Limits

- `UNVERIFIABLE_BY_REVIEW_INSTRUCTION`: supplied test, compile, JSON, and safety-scan results were not rerun.
- `UNVERIFIABLE_LIVE`: no historical or current external-call claim can be independently established by static inspection.
- M1 remains deferred and is not a condition of this round's verdict.
