# Task 7 Fix Round 1 Re-review

Date: 2026-08-11

## Review Basis

- Scope: static re-review of the Task 7 round-1 repair against `task-7-brief.md`, its Repair Rulings, the original independent review, and the exact files listed in the round-1 report appendix.
- Method: source/schema/test inspection and SHA-256 checks only. No test, compile, project, adapter, iCode, iPipe, KU, iCafe, or other live command was run, and no implementation file was edited.
- The supplied `165/165` focused result, `353/353` full-suite result, compileall exit 0, JSON checks, and clean safety scan are evidence only. The mistaken five-module command with import errors is excluded from the assessment.
- Original review SHA-256: `76beaa9f0ba7167616613b04dc18e9221a303d28a02795b2203e20f99b4d0068`.
- Current implementation report SHA-256: `f12fc146ceea87eff87e0ce267f074bbb12f825b3b3b92d4928483b2bd26448a`.
- All 23 current implementation/schema/test file hashes match the round-1 appendix exactly.
- M1 remains the approved deferred Minor finding and is outside this repair loop. Run-summary production/binding remains deferred to Task 8 under Repair Ruling 5.

## Verdicts

- Specification: **FAIL**
- Standards: **FAIL**
- Overall: **FAIL**
- Gate: **FAIL**; Task 7 must not proceed to G7/iCode approval.

Specification fails on blocking `C2`, `I1`, `I2`, `I3`, `I4`, `I5`, and new `N1`. Standards fails on blocking `I1` and `I8`. All open and new findings below are `CONFIRMED`; there are no `NEEDS_CLARIFICATION` findings and no provider-capability gap.

## Disposition Summary

| ID | Disposition |
| --- | --- |
| C1 | **ADDRESSED** |
| C2 | **NOT ADDRESSED** |
| C3 | **ADDRESSED** |
| C4 | **ADDRESSED** |
| C5 | **ADDRESSED** |
| C6 | **ADDRESSED** |
| C7 | **ADDRESSED** |
| I1 | **NOT ADDRESSED** |
| I2 | **NOT ADDRESSED** |
| I3 | **NOT ADDRESSED** |
| I4 | **NOT ADDRESSED** |
| I5 | **NOT ADDRESSED** |
| I6 | **ADDRESSED** |
| I7 | **ADDRESSED** |
| I8 | **NOT ADDRESSED** |

Addressed IDs: `C1`, `C3`, `C4`, `C5`, `C6`, `C7`, `I6`, `I7`.

Open IDs: `C2`, `I1`, `I2`, `I3`, `I4`, `I5`, `I8`.

New IDs: `N1` (Important, blocking).

## Addressed Findings

### C1 - RequirementSnapshot contract and canonical hash

**ADDRESSED** (Specification). The schema now represents the established snapshot fields, and `validate_named_schema()` invokes the production snapshot hash recomputation at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:50`. `Orchestrator.start()` also calls the production validator before persisting a supplied snapshot at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:53`.

### C3 - Generated KU identity and durable URL proof

**ADDRESSED** (Specification). Draft remote identity must be null at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:242`; completion publishes specialized content and enriches the final envelope from the verified KU/iCafe receipt at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:367`. `KnowledgeSync` returns and durably records `child_url` at `/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:174`.

### C4 - Atomic checkpoint/result commit and post-publish validation

**ADDRESSED** (Specification and Standards). Completion rechecks source event, profile, predecessor, stored artifact, and approval after publication at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:387`, then uses the transactional event CAS plus definite-result insert at `/Users/tom/Desktop/skills/tom-autodev/scripts/state_store.py:186`.

### C5 - Multi-task DAG frontier

**ADDRESSED** (Specification). Ready tasks require all dependency tasks to have passing REVIEW artifacts at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:512`; a passing review routes to the next WORKSPACE frontier until no task remains, then to SUBMIT, at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:627`.

### C6 - REVIEW result controls routing

**ADDRESSED** (Specification). Review semantic validation ties findings, axes, completeness, and verdict at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:324`. Runtime routing sends incomplete review to STOPPED, nonpassing review to DIAGNOSE, and only a complete pass onward at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:627`.

### C7 - Controller descriptors and explicit iPipe ingestion

**ADDRESSED** (Specification). Controller actions are schema-less descriptors and are rejected by the phase-result API at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:43` and `:175`. iPipe has an explicit evidence/publish/checkpoint path with a real submission predecessor and dynamic status route at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:666`.

### I6 - Atomic ArtifactStore indexing and load validation

**ADDRESSED** (Specification and Standards). The artifact row and phase index are inserted in one SQLite transaction at `/Users/tom/Desktop/skills/tom-autodev/scripts/artifact_store.py:190`; phase loads recheck index identity and the exact final envelope, specialized schema, evidence refs, remote identity, and content hash at `/Users/tom/Desktop/skills/tom-autodev/scripts/artifact_store.py:262` and `:404`.

### I7 - Stable exception allowlist

**ADDRESSED** (Standards). Only explicit dependency reason codes survive as public errors; arbitrary uppercase dependency text falls back to the stable operation code at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:16` and `:820`.

## Open Findings

### C2 - G0 still accepts incomplete intake/collaboration prerequisites

**NOT ADDRESSED** (`CONFIRMED`, Specification, Critical, blocking).

The G5/G7 portions were repaired, but G0 is still fail-open. `Orchestrator.start()` accepts `requirement_snapshot=None` and persists an INTAKE event without a snapshot or collaboration binding at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:40` through `:86`. `_collaboration_binding()` returns only profile hash, card, and role members at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:665` through `:692`; it does not bind the required group name, group owner, resolved member snapshot, group/session identity, or completed collaboration state. `_intake_prerequisites()` merely copies optional values at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:911`, and `next()` still emits an `ok: true` G0 action with null/missing prerequisites at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:122` through `:157`. This violates brief lines 71, 75, 84 and Repair Rulings 3-4.

### I1 - Exact older completion replay is not stable after same-phase re-entry

**NOT ADDRESSED** (`CONFIRMED`, Specification and Standards, Important, blocking).

Changed duplicates now conflict and the cached path performs meaningful integrity checks. However, `_validated_cached_completion()` retrieves `latest_phase(run, phase, task)` instead of the artifact ID stored in the definite result at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:434` through `:439`. After a legal later re-entry creates a newer artifact for the same phase/task, replaying the older identical completed action returns `ARTIFACT_INTEGRITY_FAILED` even when its exact stored artifact, receipt, approval, and checkpoint remain valid. The iPipe replay helper repeats the same latest-artifact lookup at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:795` through `:803`. This violates brief line 84's requirement that completed duplicate actions replay the same definite result.

### I2 - Production acceptance traceability is incompatible with the real snapshot shape

**NOT ADDRESSED** (`CONFIRMED`, Specification, Important, blocking).

The production adapter emits acceptance field values directly, including strings, at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:108` through `:120`, and the established type intentionally permits `list[Any]` at `/Users/tom/Desktop/skills/tom-autodev/scripts/requirement_snapshot.py:30` through `:44`. SPEC predecessor binding extracts IDs only from dictionary entries at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:262` through `:274`. A real snapshot such as `acceptance: ["AC-1"]` therefore produces an empty expected-ID set, while the repair fixtures invent `{id: "AP-1"}` objects at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_schema_validation.py:27`. The claimed production traceability contract is not executable.

### I3 - IMPLEMENT conflates baseline action revisions with generated candidate revisions

**NOT ADDRESSED** (`CONFIRMED`, Specification, Important, blocking).

Hash recomputation, plan bindings, and baseline checks were added. But a PLAN completion checkpoints the action's pre-implementation `source_revisions` at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:400` through `:408`; the following IMPLEMENT action inherits those revisions. IMPLEMENT then requires the generated Change Set's `content.revisions` to equal that same action value at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:311` through `:325`. This forces candidate revisions to equal plan baselines even though the schema has distinct `baseline_revisions` and `revisions`. The repair test avoids the production sequence by manually constructing the IMPLEMENT event with candidate `r2/t2` while its plan contains baseline `r1/t1` at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_phase_protocol_repair.py:914` through `:922`.

### I4 - iPipe evidence is not bound to owned pipeline/module/release policy or canonical evidence policy

**NOT ADDRESSED** (`CONFIRMED`, Specification, Important, blocking).

Ingestion compares revisions, environment fingerprint, submission artifact, and G7 approval at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:697` through `:710`, but never compares `pipeline_id`, `module`, or `release_rule` with the pinned project profile or controller event. The profile owns these identities (`pipeline_profile.pipeline_id`, repository modules, and `pipeline_profile.release_rule`) at `/Users/tom/Desktop/skills/tom-autodev/schemas/project-profile.schema.json:42` and `:100`. `_validate_ipipe()` checks only status/stage/job consistency at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:369`; job evidence and `remote_evidence_refs` are merely nonempty strings in `/Users/tom/Desktop/skills/tom-autodev/schemas/ipipe-evidence.schema.json:1`, never validated through the canonical/secret evidence policy. Contradictory or unsafe pipeline evidence can still drive release routing.

### I5 - DECISIONS_RECORDED remains shallow and contradictory states are accepted

**NOT ADDRESSED** (`CONFIRMED`, Specification, Important, blocking).

Spec substantive-section checks were added and run-summary is correctly deferred. The decision-log validator, however, handles only `NO_OPEN_DECISIONS` and `UNRESOLVED` at `/Users/tom/Desktop/skills/tom-autodev/scripts/schema_validator.py:241` through `:250`. The schema permits `DECISIONS_RECORDED` with an empty decisions list, arbitrary COMPLETE/INCOMPLETE status/frontier combinations, and a `choice` absent from its `options` at `/Users/tom/Desktop/skills/tom-autodev/schemas/decision-log.schema.json:1`. GRILL can therefore claim a completed decision result without a coherent decision.

### I8 - Repair tests still omit production-shaped negative and recovery cases

**NOT ADDRESSED** (`CONFIRMED`, Standards, Important, blocking).

The round adds substantial meaningful coverage, but the fixtures still use invented acceptance objects and the traceability test extends that invented shape at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_phase_protocol_repair.py:858`. Existing orchestrator tests continue to start runs without any snapshot at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:91`. No focused test covers missing/null complete G0 prerequisites, full collaboration/session binding, production string acceptance traceability, a normal PLAN-baseline to generated-candidate revision sequence, owned pipeline/module/release-rule mismatches, canonical rejection of nested iPipe evidence refs, older exact replay after same-phase re-entry, or preservation of draft evidence in the final envelope. The supplied green counts therefore do not cover the remaining blockers.

## New Fix-Round Finding

### N1 - Final envelopes discard the phase result's validated evidence references

- Classification: `CONFIRMED`
- Axis: Specification
- Severity: Important
- Blocking: Yes
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:235`, `:376`, `:711`, `:728`, and `:922`
- Affected requirement: brief lines 26, 49, 71, 88, and 91.

The draft's `evidence_refs` are validated and are part of the accepted result at `/Users/tom/Desktop/skills/tom-autodev/scripts/phase_protocol.py:235` through `:241`. Finalization then replaces that list wholesale with only KU/index/iCafe receipt refs at `:376` through `:383`; iPipe ingestion does the same at `:711` through `:731`. `_source_evidence_refs()` propagates the predecessor final envelope's list at `:922` through `:930`, so the evidence supplied by the child phase or iPipe jobs disappears from the canonical phase chain. The repair introduced this loss while separating draft content from generated publication identity. Final evidence must preserve the validated phase/source refs together with canonical publication receipts.

## Required Acceptance For The Next Repair

1. Starting or advancing INTAKE with a missing snapshot, missing group name/owner/member snapshot, or incomplete collaboration/session binding must fail before an `ok` G0 action; a complete binding must be fully included in the G0 input hash.
2. A production-shaped snapshot with `acceptance: ["AC-1"]` must accept a Spec tracing `AC-1`, reject omission/unknown IDs, and retain hash validation.
3. A normal PLAN at baselines `r1/t1` followed by IMPLEMENT producing candidate revisions `r2/t2` must validate; unrelated candidate identities must fail without pre-seeding candidate revisions into the IMPLEMENT event.
4. iPipe ingestion must reject pipeline ID, module, release rule, revisions, environment, submission, or G7 values that differ from pinned owned inputs, and must reject noncanonical/secret refs in both jobs and remote evidence.
5. `DECISIONS_RECORDED` must require coherent nonempty decisions, completed status, resolved frontier, unique IDs, and choices drawn from declared options.
6. After a later valid same-phase/task artifact exists, replay of an older identical phase or iPipe action must load and validate the exact artifact recorded in its definite result and return that original result; changed input and exact-artifact corruption must still fail.
7. Final phase and iPipe envelopes must retain the validated draft/source evidence refs plus canonical KU/iCafe receipt refs, and the next action must observe the preserved set.
8. Focused tests must exercise every case above through production-shaped orchestrator/protocol flows rather than manually seeding states that bypass the binding being tested.

## Verification Limits

- `UNVERIFIABLE_BY_REVIEW_INSTRUCTION`: the supplied test, compile, JSON, and safety-scan results were not rerun.
- `UNVERIFIABLE_LIVE`: no historical or current live external-call claim can be independently established by static inspection.
- M1 remains deferred and is not a condition of this round's verdict.
