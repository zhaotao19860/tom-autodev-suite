# Task 7: Phase Protocol and Specialized Artifact Schemas

## Context

Build the deterministic Comate-host phase boundary that connects the completed state, approval, artifact, KU/iCafe, collaboration, iCode, and iPipe control-plane foundations. This task defines actions and validates/persists phase results; it does not execute child Skills or any BGW/XFlow build/test command.

## Files

- Create `scripts/phase_protocol.py`.
- Extend the existing `scripts/schema_validator.py`; preserve project-profile validation behavior.
- Create `schemas/requirement-snapshot.schema.json`, `decision-log.schema.json`, `spec.schema.json`, `task-dag.schema.json`, `task-plan.schema.json`, `change-set.schema.json`, `review.schema.json`, `diagnosis.schema.json`, `ipipe-evidence.schema.json`, `run-summary.schema.json`, and `optimization-proposal.schema.json`.
- Modify `scripts/orchestrator.py` and `scripts/artifact_store.py` only as required to construct/use the protocol and atomically index validated phase artifacts.
- Create `scripts/tests/test_phase_protocol.py` and `scripts/tests/test_schema_validation.py`; update existing control-plane tests only when an established contract is deliberately extended.

## Public Interfaces

- `PhaseProtocol.next(run_id: str) -> dict`
- `PhaseProtocol.validate_result(action: dict, result: dict) -> dict`
- `PhaseProtocol.complete(run_id: str, envelope: dict) -> dict`
- Add deterministic named-schema loading/validation to `schema_validator.py` without breaking `validate_schema(instance, schema)` or `load_project_profile_schema()`.

Every failure is a structured result with `ok: false` and a stable `reason_code`; expected validation failures do not escape as raw `KeyError`, `TypeError`, JSON, SQLite, or filesystem exceptions.

## Artifact Envelope

Every accepted result becomes one canonical envelope with exactly validated data and at least:

```json
{
  "run_id": "string",
  "phase": "string",
  "task_id": "string-or-null",
  "schema_version": "1",
  "input_hash": "64-lowercase-hex",
  "content_hash": "64-lowercase-hex",
  "source_revisions": {},
  "parent_artifact_hash": "64-lowercase-hex-or-null-only-for-root",
  "knowledge_doc_id": "string",
  "knowledge_url": "canonical-ku-url",
  "evidence_refs": [],
  "approval_id": "string-or-null",
  "content": {}
}
```

- Canonical JSON is UTF-8, sorted keys, compact separators, and excludes derived `content_hash`; recompute and compare every hash instead of trusting caller flags.
- `input_hash` binds the exact `PhaseAction` inputs. `parent_artifact_hash` must equal the current predecessor artifact hash; only the requirement snapshot root may use null.
- `source_revisions` must include the exact run-bound business and independent test repository revision identities whenever the phase is task/code/pipeline related. Empty optional objects cannot satisfy a required revision contract.
- `evidence_refs` must be canonical, non-secret, nonempty whenever the phase requires evidence, and are validated through existing persistence policy.
- Non-root phase completion requires a verified published KU receipt with exact content hash, doc ID, canonical URL/version evidence, plus the matching idempotent iCafe comment receipt. Missing or mismatched KU/iCafe evidence is incomplete, not success.
- The envelope and specialized content schema both use `additionalProperties: false` where a free-form evidence/value map is not explicitly intended. Schema errors are deterministic and sorted by path/kind.

## Specialized Schemas

- `requirement-snapshot`: canonical iCafe card identity, title/body/html, acceptance points, fields, attachments/links, status/type, responsible people, created/modified metadata, and canonical snapshot hash.
- `decision-log`: decision result/status, ordered decisions with question/options/choice/rationale/evidence, unresolved frontier, glossary delta, ADR candidates, and source evidence. `NO_OPEN_DECISIONS` must be explicit.
- `spec`: version, numbered behaviors, Given/When/Then acceptance scenarios, boundaries/errors, compatibility, test interface, environment requirements, non-goals, risks, rollback, release evidence, and traceability from every iCafe acceptance point.
- `task-dag`: independently verifiable capability-slice nodes, acyclic edges, business/test repository changes per node, test IDs/fixtures/iPipe stages/completion predicate, and complete acceptance coverage.
- `task-plan`: exactly one `task_id`; exact repositories/files/modules/symbols/interfaces, tests/fixtures/assertions, iPipe parameters, risks, rollback, ordered checklist, and G4-bound identity. Plans are task-specific, never one aggregate placeholder.
- `change-set`: one task/change-set identity, exact baseline and complete business/test patches/revisions, full diff identity, test IDs, traceability delta, and candidate hash for G5. It records generated test code but no local execution evidence.
- `review`: fixed baseline/change-set hash, Standards and Spec axes, classified findings with severity/location/evidence/acceptance/blocking, verdict, provider identity, and completeness state.
- `diagnosis`: frozen business/test revisions, build/stage/job/environment/log evidence, classification, reproduction/comparison, exactly one falsifiable hypothesis, minimal verification, route, repair direction/plan/diff identity, and attempt/failure signature. Insufficient evidence is explicit.
- `ipipe-evidence`: exact owned pipeline/build/module, complete revision set, nonempty stage/job evidence, environment fingerprint, status/classification/failure signature, release rule/evidence when applicable, and canonical remote evidence refs.
- `run-summary`: terminal/result state, phase timings, human waits, retries, review findings, failure signatures/classes, resolutions, missing knowledge, repeated manual operations, collaboration/external receipt references, and redacted metrics.
- `optimization-proposal`: proposal/candidate hash, evidence/root cause, target files under allowed Skill/control-plane roots, candidate diff, expected benefit, risk, rollback, exact verification commands, status and G10 approval binding. It cannot target business repositories, project profiles, iPipe templates, credentials, or production state.

Use JSON Schema Draft 2020-12-compatible files, while extending the local standard-library validator only for the schema features actually used. Do not add an undeclared third-party runtime dependency.

## Phase Action Contract

`next()` reads the durable run checkpoint, current/pinned profile identity, latest valid artifacts and task frontier. It returns one exact Comate-only action containing `run_id`, current state, phase, child Skill name, task ID where applicable, immutable input artifact references/hashes, canonical `input_hash`, required human gate, allowed side effects, completion predicate, and `host: "comate"`.

Required child mapping:

- `INTAKE` -> requirement snapshot/collaboration prerequisites, then `tom-grill` for `GRILL` under G0.
- `GRILL` -> `tom-grill`, producing `decision-log`, G1.
- `SPEC` -> `tom-spec`, producing `spec`, G2.
- `TASKS` -> `tom-tasks`, producing `task-dag`, G3.
- each frontier `PLAN` -> `tom-plan`, producing one `task-plan`, G4.
- each `IMPLEMENT` -> `tom-implement`, producing one `change-set`, bound to the approved task plan.
- each `REVIEW` -> `tom-review`, producing one `review`, fixed to the same baseline/change set.
- `DIAGNOSE` -> `tom-diagnose`, producing one `diagnosis`, G6 before repair.

For controller-owned `WORKSPACE`, `SUBMIT`, `IPIPE`, `RELEASE`, terminal/blocked states, return the exact controller action/stop result rather than inventing a child Skill. Never return a phase action that violates `TransitionPolicy`, lacks its predecessor, has an unresolved pending external intent, uses a stale profile/artifact/action hash, or crosses task identities. Completed/duplicate actions replay the same result; changed input under the same action identity is a conflict.

## Completion Contract

1. `validate_result()` verifies action identity, Comate host metadata, current run/state/task, exact input and parent hashes, specialized schema, source revisions, evidence refs, approval/gate binding, and canonical content hash. Reject stale or cross-run results before any write.
2. `complete()` must be idempotent and fail closed. Persist the validated local artifact, publish the immutable phase document through the existing `KnowledgeSync`, verify KU and iCafe receipts, then make the legal transition/checkpoint from the state/action that was validated.
3. Model the sequence with durable intent/result/checkpoint data so a crash after local artifact write or remote publish resumes/query-reconciles without duplicate KU documents, comments, artifacts, or transitions. A partially completed sequence returns recovery/query required and never claims completion.
4. Do not report completion unless ArtifactStore integrity, specialized schema, KU receipt, iCafe receipt, action hash, current source event/checkpoint, and any required ledger-backed approval all still match.
5. Requirement change/stale predecessor invalidates downstream actions and approvals and routes back through the existing transition/failure policy; it must not silently overwrite history.

## Existing Contracts To Reuse

- Reuse `StateStore`, `ArtifactStore`, `KnowledgeSync`, `ApprovalLedger`, `EvidenceGate`, `TransitionPolicy`, `persistence_policy`, pinned profile evidence, and existing canonical reason/evidence conventions. Do not build parallel persistence, approval, or KU/iCafe clients.
- KU tree names remain `00-requirement-snapshot`, `01-grill`, `02-spec`, `03-tasks`, `04-task-plan/<task-id>`, `05-change-set/<task-id>`, `06-review/<task-id>`, `07-diagnosis/<attempt>`, `08-ipipe-evidence/<build-id>`, `09-run-summary`, `10-optimization-proposal`.
- G0-G10 approvals bind canonical input hashes; input changes expire prior approval. Comate and Infoflow share the approval ledger from Task 5.
- Mac never runs BGW/XFlow compilation, unit, regression, integration, Docker, NCS, simulator, or release commands. Tests use fake KnowledgeSync/adapters and temporary state/artifact directories only.
- `tom-autorelease` remains source material only and must not be imported, executed, or resolved at runtime. Do not add Codex metadata or compatibility.

## Required TDD and Verification

1. Before production edits, add focused RED tests for phase ordering and exact child mapping; task-specific plans/frontier identity; missing/wrong parent hash; stale/cross-run action/input; wrong specialized schema; content/source-revision mismatch; missing/mismatched KU or iCafe receipt; pending intent/recovery; duplicate completion; approval mismatch; terminal/illegal states; and `host` other than `comate`.
2. Add schema tests with at least one valid artifact and deterministic missing/invalid-path cases for every specialized schema, including DAG cycle/coverage semantic checks and optimization forbidden-target checks.
3. Use fake transports only. Do not make live iCafe/KU/Infoflow/iCode/iPipe calls or execute project commands.
4. Run focused phase/schema tests, affected orchestrator/artifact/knowledge tests, full `scripts/tests` discovery, `py_compile`/`compileall`, and scans proving no Codex/tom-autorelease/local project command/runtime dependency was introduced.
5. The workspace is not a Git repository. Do not initialize Git or fabricate commits.

## Repair Rulings After Independent Review

These rulings clarify causal ordering; they preserve the final ArtifactEnvelope contract rather than weakening it.

1. A child phase returns a validated pre-publication draft. In that draft, `knowledge_doc_id` and `knowledge_url` are null because KU creates their real values. `complete()` publishes canonical phase content, verifies the real KU/iCafe receipt, then constructs and stores the final ArtifactEnvelope with non-null exact doc ID/URL/version evidence. A caller may never predict or supply the final remote identity.
2. `content_hash` is the SHA-256 of canonical specialized phase `content`, not of publication metadata. The phase chain separately binds `action_id`, `input_hash`, `parent_artifact_hash`, `source_revisions`, `task_id`, and `content_hash`. Final remote identity and approval are immutable envelope metadata and remain covered by ArtifactStore integrity/identity.
3. Human approval for a generated phase artifact binds a canonical `approval_input_hash` derived from action identity, task/parent/source revisions, and the validated `content_hash`. G0 is the exception: it binds the full canonical iCafe/project/collaboration prerequisites already present in the INTAKE action. G5 binds the complete candidate Change Set; G7 is used only by controller-owned iCode submit/patchset, never by `tom-review` output.
4. The INTAKE action is controller-owned and publishes the established `RequirementSnapshot` already captured from iCafe; it is not produced by `tom-grill`. Only after G0 and immutable snapshot/collaboration completion does the GRILL state invoke `tom-grill`.
5. Controller-owned WORKSPACE/SUBMIT/IPIPE/RELEASE actions are descriptors for their existing runtimes, not phase-result actions passed to `validate_result()`/`complete()`. IPIPE evidence ingestion must have an explicit artifact/checkpoint path with a real predecessor and dynamic legal route. RELEASE must verify release evidence; `run-summary` remains Task 8 post-run scope and is not a RELEASE schema.
6. For a multi-node DAG, each ready node is selected only after all dependency nodes have a complete passing REVIEW artifact. A passing REVIEW routes to the next task's WORKSPACE/PLAN frontier while tasks remain, and to SUBMIT only when all DAG nodes are complete. Rejected/blocking review routes to DIAGNOSE; incomplete review stops with explicit review-incomplete evidence.
