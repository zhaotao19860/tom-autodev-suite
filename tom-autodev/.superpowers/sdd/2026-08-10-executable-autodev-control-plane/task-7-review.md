# Task 7 Independent Review

Date: 2026-08-11

## Review Basis

- Exact requirement: `task-7-brief.md`.
- Design reference: phase protocol, G0-G10, recovery, external-write, state-routing, environment/test, and optimization sections of `2026-08-10-executable-autodev-control-plane-design.md`.
- Non-Git baseline: `task-7-baseline.md`.
- Implementer evidence: `task-7-report.md` and the current files named by that report.
- Review method: static inspection only. No test, compile, project, adapter, or live command was run.
- Current SHA-256 values for all 17 reported changed files match the implementation report. The five reused files checked from the baseline (`state_store.py`, `knowledge_sync.py`, `transition_policy.py`, `evidence_policy.py`, and `evidence_gate.py`) still match their baseline hashes. Because the workspace has no Git history and the baseline contains hashes rather than file snapshots, the exact textual diff of the three modified files is not independently reconstructible.
- The reported fresh `22/22` focused tests, `297/297` full tests, compileall exit 0, and clean scans are treated as supplied evidence only and were not rerun.

## Verdicts

### Specification: FAIL

Seven blocking Critical findings and seven blocking Important specification findings are confirmed. The implementation cannot accept the established iCafe snapshot, cannot execute a real KU publish with a pre-bound generated document identity, cannot safely recover the final checkpoint crash window, cannot sequence a multi-node task DAG, can advance rejected/incomplete reviews, and advertises controller actions that cannot satisfy their own completion contract.

### Standards: FAIL

The implementation has blocking correctness and test-quality gaps in atomic state commit, cross-artifact validation, durable idempotency, ArtifactStore indexing, stable error propagation, and integration-shaped fakes. The supplied passing counts do not cover these paths.

### Overall: FAIL

Task 7 must not proceed to G7/iCode. All findings below are `CONFIRMED`; there are no provider suggestions requiring `NEEDS_CLARIFICATION` classification. Historical claims about the order of RED/GREEN edits and absence of past live calls remain unverifiable from the static workspace and are not treated as findings.

## Critical Findings

### C1 - The requirement-snapshot schema rejects the established runtime snapshot and does not verify its canonical hash

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: `schemas/requirement-snapshot.schema.json:1` requires `acceptance_points`, string `responsible_people`, `created_at`, `modified_at`, and `snapshot_hash`. The established contract requires `acceptance`, person objects, structured `created`/`modified`, and `content_hash` (`scripts/requirement_snapshot.py:10-44`), and `CafeClient` emits exactly that established shape (`scripts/clients/icafe_client.py:115-135`). Both shapes reject additional properties. The Task 7 fixture uses only the invented shape (`scripts/tests/test_schema_validation.py:20-26`). The new schema checks only a 64-hex pattern; `validate_result()` never calls the established recomputation in `requirement_snapshot.validation_error()` (`scripts/requirement_snapshot.py:47-62`).
- Affected requirement: brief lines 53-55, 71, 75, 88, 96; preserve and reuse the existing iCafe RequirementSnapshot contract and exact canonical snapshot hash.
- Impact: a real iCafe snapshot cannot complete INTAKE, while a fabricated `snapshot_hash` is accepted. This blocks the first phase and breaks the root of every downstream hash and traceability chain.

### C2 - G0/G5/G7 approval identities do not bind the objects they are supposed to approve

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: INTAKE is emitted as a `tom-grill` child with no requirement/collaboration input artifacts (`scripts/phase_protocol.py:25-33,114-146`), although G0 approves the iCafe card, project, collaboration group, and member list. The initial event payload is represented in the hash only by an opaque event UUID, not by those canonical objects. IMPLEMENT is assigned G5 before the resulting candidate diff exists, so its `input_hash` binds the task plan, not the candidate diff (`scripts/phase_protocol.py:30-32,116-127`). REVIEW then consumes G7, and SUBMIT also consumes G7 (`scripts/phase_protocol.py:32,38`); the focused test hard-codes REVIEW/G7 (`scripts/tests/test_phase_protocol.py:121-123`). The established evidence contract instead identifies REVIEW with G5 and SUBMIT/IPIPE with G7 (`scripts/evidence_policy.py:23-28`). `validate_result()` checks approval input only against the pre-result action hash (`scripts/phase_protocol.py:375-403`).
- Affected requirement: brief lines 71-84 and 88; design lines 275-291; G0 approves intake/collaboration, G5 the complete candidate diff, and G7 the iCode submission/patchset. The brief explicitly requires INTAKE prerequisites before `tom-grill` under G0.
- Impact: approvals can be valid for inputs that do not contain the reviewed artifact, REVIEW consumes an iCode gate, and the actual candidate diff is never the G5-bound object. Human gates no longer provide the promised immutable authorization boundary.

### C3 - A real KnowledgeSync publish cannot satisfy the pre-publication envelope identity

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: `content_hash` covers every envelope field except itself (`scripts/phase_protocol.py:216-218,451-452`), including caller-supplied `knowledge_doc_id` and `knowledge_url`. The local artifact is stored before `KnowledgeSync.publish_phase()` (`scripts/phase_protocol.py:261-273`). The real KU document ID and URL are only created/returned during `create_artifact()` (`scripts/knowledge_sync.py:130-139`; `scripts/clients/ku_client.py:183-200,225-258`). PhaseProtocol then demands that this generated ID equal the already-hashed caller value (`scripts/phase_protocol.py:405-425`). The fake hides the circular dependency by always returning the fixture's `doc-1` (`scripts/tests/test_phase_protocol.py:33-47,97-99`). In addition, KnowledgeSync drops the child URL from its final receipt (`scripts/knowledge_sync.py:174-185`), so `_receipt_error()` cannot compare the published canonical URL to the envelope.
- Affected requirement: brief lines 24-50 and 86-91; design lines 126-149 and 314-333. The accepted envelope must be canonical, then publication must prove the exact KU doc ID, URL, version, and hash plus the matching iCafe comment.
- Impact: without an unauthorized pre-created KU document or a predictable remote ID, no production completion can succeed. Even a contrived matching ID lacks exact post-publish URL proof.

### C4 - Completion has no atomic source-event/checkpoint commit and cannot recover a crash after transition

- Axis: Specification and Standards
- Severity: Critical
- Blocking: Yes
- Evidence: validation reads the current event before the remote publish (`scripts/phase_protocol.py:176-185,258-273`), but after the external call it does not re-read the source event, predecessor, artifact integrity, profile, or approval before transition. `StateStore.transition()` is unconditional and has no compare-and-swap expected event (`scripts/state_store.py:111-133`). `_complete()` appends the transition and only afterward stores the idempotency result in a separate transaction (`scripts/phase_protocol.py:279-310`). A crash between lines 283-303 and 310 leaves the new state durable but no completion result; retry calls `next()` for the new state and cannot replay the old envelope. A concurrent transition during KU/iCafe work can likewise be followed by a stale transition.
- Affected requirement: brief lines 84 and 88-92; design lines 293-333. Completion must transition from the exact validated checkpoint and recover after artifact write, publish, transition, and before idempotency-result persistence without duplicate transition or loss of the definite result.
- Impact: stale work can advance the state machine, and the explicitly required post-transition crash window is unrecoverable. This can strand a successfully published phase or append an illegal second history branch.

### C5 - The protocol cannot execute a multi-task DAG frontier

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: PLAN chooses the first list node without a PLAN artifact, ignoring DAG edges and whether predecessor tasks completed (`scripts/phase_protocol.py:318-329`). After one PLAN/IMPLEMENT/REVIEW, the fixed transition path is REVIEW -> SUBMIT -> IPIPE -> RELEASE, and the unchanged policy has no transition back to PLAN for remaining nodes (`scripts/transition_policy.py:11-21`). The only frontier test uses a one-node DAG and checks a single selection (`scripts/tests/test_phase_protocol.py:165-174`).
- Affected requirement: brief lines 58-60, 71, 79-84 and 104-105; design lines 229-239 and 335-356. Every ready DAG frontier task needs its own plan/change-set/review while respecting dependencies and complete acceptance coverage.
- Impact: a multi-node requirement either stops after the first task or selects a dependency-blocked node. Remaining capability slices never receive plans, code, tests, or review.

### C6 - REVIEW verdict, completeness, and blocking findings do not control routing

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: the review schema permits `REJECT`, `INCOMPLETE`, incomplete axes, and blocking findings (`schemas/review.schema.json:1`). `_completion_target()` inspects only DIAGNOSE content; every REVIEW result uses the static SUBMIT target (`scripts/phase_protocol.py:428-436`, with target declared at line 32). There is no cross-field validation between finding IDs, axis completeness, verdict, and blocking state.
- Affected requirement: brief lines 61, 81, 84, 88-92; design lines 241-247 and 346-354. Blocking review findings must route to DIAGNOSE, incomplete review must stop/request provider or human review, and only a complete passing review may reach submission.
- Impact: a schema-valid rejected or incomplete review with P0 blocking findings is published as complete and advances to SUBMIT.

### C7 - The IPIPE and RELEASE controller actions are internally uncompletable, and RELEASE prematurely substitutes run-summary

- Axis: Specification
- Severity: Critical
- Blocking: Yes
- Evidence: IPIPE declares `ipipe-evidence` but no predecessor and no target (`scripts/phase_protocol.py:39`). Its action therefore has a null parent, which `validate_result()` rejects for every non-INTAKE phase (`scripts/phase_protocol.py:201-209`); if bypassed, completion raises `COMPLETION_TARGET_REQUIRED` (`scripts/phase_protocol.py:428-436`). RELEASE also has no target and incorrectly declares `run-summary` as its phase schema/title (`scripts/phase_protocol.py:40,43-48`), even though run-summary belongs after terminal release success. The controller test checks only names and never validates or completes either action (`scripts/tests/test_phase_protocol.py:141-163`).
- Affected requirement: brief lines 63-65, 84, 89-91 and 97; design lines 335-395. Controller-owned IPIPE/RELEASE must return exact bounded controller actions and dynamic legal outcomes; run-summary is a post-terminal artifact, not RELEASE evidence.
- Impact: advertised controller contracts cannot be completed through the public protocol, iPipe evidence cannot enter the artifact chain, and release is asked to produce a terminal summary before release succeeds.

## Important Findings

### I1 - Duplicate completion accepts changed input and can replay success after integrity loss

- Axis: Specification and Standards
- Severity: Important
- Blocking: Yes
- Evidence: `_complete()` returns a cached result by `run_id/action_id` before validating the supplied envelope (`scripts/phase_protocol.py:243-252`). It does not compare the duplicate envelope hash. The cached path also does not recheck ArtifactStore integrity, schema, receipts, current source event, or approval. The test repeats only the identical object (`scripts/tests/test_phase_protocol.py:257-268`).
- Affected requirement: brief lines 84, 90-91 and 104. Identical duplicates replay; changed input under the same action identity conflicts, and completion must not be claimed after required integrity becomes false.
- Impact: a modified envelope with a previously successful action ID receives the old success, and later artifact/index corruption remains hidden.

### I2 - Cross-artifact traceability and DAG coverage are self-declared rather than checked against predecessors

- Axis: Specification
- Severity: Important
- Blocking: Yes
- Evidence: `validate_result()` supplies only content and schema name to the validator and performs no predecessor-aware semantic validation (`scripts/phase_protocol.py:192-215`). Spec trace entries are not checked against the RequirementSnapshot acceptance IDs, behavior IDs, or scenario IDs (`schemas/spec.schema.json:1`). DAG coverage compares only IDs self-declared inside the DAG; it does not detect duplicate task IDs, prove coverage of predecessor Spec/iCafe acceptance points, or ensure a coverage task is the node that declares that acceptance (`scripts/schema_validator.py:145-199`).
- Affected requirement: brief lines 55-60, 88, 91-92 and 105; design lines 225-235 and 360-374.
- Impact: omitted acceptance points, nonexistent behavior/scenario references, duplicate tasks, and false task-to-acceptance mappings can all be accepted and published.

### I3 - Task-plan and change-set schemas do not enforce their exact business/test and hash bindings

- Axis: Specification
- Severity: Important
- Blocking: Yes
- Evidence: task-plan permits a single repository of either role; the valid fixture contains only the business repository (`schemas/task-plan.schema.json:1`; `scripts/tests/test_schema_validation.py:49-57`). There is no check that `g4_input_hash` equals the relevant action/approval, that checklist orders are unique/ordered, or that test IDs/fixtures match the DAG. Change-set revision fields are not compared with envelope/action revisions or plan baselines, and `full_diff_hash`/`candidate_hash` are accepted as arbitrary 64-hex strings without recomputation from both patches and revisions (`schemas/change-set.schema.json:1`; `scripts/phase_protocol.py:192-218`).
- Affected requirement: brief lines 48, 58-61, 88 and 98; design lines 233-243 and 275-291.
- Impact: a plan can omit the independent test repo; a change-set can claim unrelated revisions, patches, candidate identity, and G4/G5 bindings.

### I4 - Review, diagnosis, and iPipe schemas lack required semantic consistency

- Axis: Specification
- Severity: Important
- Blocking: Yes
- Evidence: review change-set hash/baselines are not matched to the predecessor and axis finding IDs need not name findings (`schemas/review.schema.json:1`). Diagnosis always requires a hypothesis and repair diff even when `evidence_state` is `INSUFFICIENT`, yet an insufficient result can still choose `REPAIR`; it has no explicit `DIAGNOSIS_INCOMPLETE` outcome (`schemas/diagnosis.schema.json:1`; `scripts/phase_protocol.py:428-435`). iPipe permits SUCCESS with a failure signature, FAILURE without one, arbitrary stage/job statuses, stage job IDs absent from `jobs`, empty release evidence, and content revisions/environment unrelated to the envelope/profile (`schemas/ipipe-evidence.schema.json:1`).
- Affected requirement: brief lines 61-64, 88 and 105; design lines 241-247, 346-360 and 374.
- Impact: contradictory evidence artifacts can pass and drive incorrect repair/release decisions.

### I5 - Required sections are often only present, not deep or nonempty

- Axis: Specification
- Severity: Important
- Blocking: Yes
- Evidence: the spec `$defs/nonemptyStrings` omits `minItems`, so boundaries, errors, compatibility, test interface, environment requirements, non-goals, risks, rollback, and release evidence may all be empty (`schemas/spec.schema.json:1`). `NO_OPEN_DECISIONS` has no consistency rule with decisions/frontier/status (`schemas/decision-log.schema.json:1`). Run-summary uses unrestricted objects for waits, retries, and review findings and is not tied to durable run events (`schemas/run-summary.schema.json:1`). Schema tests remove only the first top-level key and add one unexpected key; they do not exercise these semantics (`scripts/tests/test_schema_validation.py:119-131`).
- Affected requirement: brief lines 56-65, 67 and 105.
- Impact: nominally complete artifacts can omit the substantive data that gates downstream work and post-run analysis.

### I6 - ArtifactStore indexing is not atomic with artifact persistence and does not revalidate specialized content

- Axis: Standards and Specification
- Severity: Important
- Blocking: Yes
- Evidence: `put_envelope()` first calls `put()`, whose artifact row/file transaction completes, then opens a second transaction for `phase_artifacts` (`scripts/artifact_store.py:145-195`). A crash or index conflict can leave an unindexed base artifact. The method trusts the caller's “already validated” claim and rechecks only the envelope hash; `_load_phase()` verifies hashes/index identity but not the named schema, evidence policy, or envelope exact key set (`scripts/artifact_store.py:227-248`). The sole integrity test edits one index phase (`scripts/tests/test_phase_protocol.py:324-339`).
- Affected requirement: brief lines 12, 51, 89-91 and 96; atomically index validated artifacts and continue to enforce integrity.
- Impact: artifact and phase index state can diverge, and direct/recovered indexed content can remain hash-consistent while violating its specialized schema.

### I7 - Stable exception reason codes are inconsistently preserved

- Axis: Standards
- Severity: Important
- Blocking: Yes
- Evidence: `next()` converts every dependency exception, including a definite `ValueError("INTENT_CONFLICT")` or ArtifactStore integrity reason, to `PHASE_PROTOCOL_INVALID` (`scripts/phase_protocol.py:69-73`). `validate_result()` and `complete()` accept any all-uppercase exception text as a public reason code rather than an explicit allowlist (`scripts/phase_protocol.py:157-163,235-241`). The only exception test covers a generic RuntimeError during completion (`scripts/tests/test_phase_protocol.py:295-306`).
- Affected requirement: brief line 22 and report claim at lines 19 and 64. Failures need deterministic, canonical reason codes without erasing definite causes or exposing arbitrary dependency messages as contract values.
- Impact: actionable recovery/conflict causes are lost in `next()`, while unrelated uppercase exception text can destabilize the public API.

### I8 - Focused tests encode wrong contracts and omit the highest-risk crash/integration cases

- Axis: Standards
- Severity: Important
- Blocking: Yes
- Evidence: tests hard-code the incompatible snapshot and REVIEW/G7 mapping (`scripts/tests/test_schema_validation.py:20-26`; `scripts/tests/test_phase_protocol.py:106-123`), use only a one-node DAG (`scripts/tests/test_phase_protocol.py:165-174`), and use a fake that returns the caller-predicted KU doc ID while omitting URL proof (`scripts/tests/test_phase_protocol.py:33-47`). There are no tests for changed duplicate envelopes, crash after transition/before completion-result save, source event changing during publish, artifact corruption before duplicate replay, real RequirementSnapshot validation, two-node frontier/dependency sequencing, REVIEW reject/incomplete/blocking routing, IPIPE/RELEASE completion, cross-artifact traceability, content-vs-envelope revisions, candidate/diff hash recomputation, diagnosis insufficient consistency, or success/failure iPipe consistency.
- Affected requirement: brief lines 102-107.
- Impact: the supplied green suite validates the implementation's assumptions rather than the specified end-to-end contracts and cannot support the COMPLETE claim.

## Minor Finding

### M1 - Diagnosis KU path always uses attempt 1

- Axis: Specification
- Severity: Minor
- Blocking: No by itself
- Evidence: `_phase_title()` always returns `07-diagnosis/1` and ignores the schema's `attempt` value (`scripts/phase_protocol.py:539-546`).
- Affected requirement: brief line 97, which requires `07-diagnosis/<attempt>`.
- Impact: repeated diagnosis artifacts use the wrong KU tree identity and risk immutable-title conflicts or misleading history.

## Supplied Evidence Assessment

- Current changed-file hashes match `task-7-report.md`; no undeclared change was found among the baseline files checked.
- The reported 22 focused and 297 full passing tests establish that the current asserted behavior is internally green, not that the missing negative and integration contracts above are satisfied.
- The reported compile/json/scans are credible as supplied evidence and no prohibited import/command string was found in the inspected changed files. This does not independently prove no live call was made historically.
- No third-party runtime dependency, Codex compatibility, `tom-autorelease` runtime reference, Git initialization, or local BGW/XFlow execution API was observed in the changed implementation.

## Required Disposition

Return the fixed baseline to diagnosis/repair. A repair must first reconcile the phase/gate model with the existing EvidencePolicy/TransitionPolicy, use the real RequirementSnapshot, redesign envelope enrichment/publication so remote identity is verifiable without prediction, make the final checkpoint atomic/recoverable, define multi-task frontier transitions, and add the missing semantic/integration tests before another independent review.
