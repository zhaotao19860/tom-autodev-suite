# Task 7 Implementation Report

Date: 2026-08-11

## Status

COMPLETE. The deterministic Comate-only `PhaseProtocol`, strict specialized artifact schemas, named-schema validation, phase artifact indexing, and orchestrator construction boundary are implemented. No live external write or Mac-side BGW/XFlow project command was executed.

## Requirements Checklist

- [x] Added `PhaseProtocol.next(run_id)`, `validate_result(action, result)`, and `complete(run_id, envelope)`.
- [x] Added exact Comate-only child mappings for INTAKE, GRILL, SPEC, TASKS, PLAN, IMPLEMENT, REVIEW, and DIAGNOSE.
- [x] Added controller actions for WORKSPACE, SUBMIT, IPIPE, and RELEASE, plus terminal stop results.
- [x] Bound actions to current event, pinned profile hash/file, predecessor artifact, task frontier, source revisions, and canonical input hash.
- [x] Rejects pending external intents, missing/corrupt predecessors, stale/forged/cross-run/cross-task actions, wrong host, profile changes, and illegal states.
- [x] Added canonical envelope validation with an exact key set, schema version 1, SHA-256 recomputation excluding derived `content_hash`, canonical KU URL, persistence-safe evidence refs, and ledger-backed gate approval.
- [x] Added non-root predecessor enforcement and task/code/pipeline business+test source revision enforcement.
- [x] Added local artifact persistence, immutable phase index, index/file/envelope integrity revalidation, KU/iCafe receipt verification, legal transition checkpointing, and duplicate completion replay.
- [x] Completion fails closed on partial/pending external operations and converts dependency/validation/filesystem/SQLite failures to structured results.
- [x] Reused `StateStore`, `ArtifactStore`, `KnowledgeSync`, `ApprovalLedger`, `EvidenceGate`, `TransitionPolicy`, and persistence policy. No parallel client or store was introduced.
- [x] Extended schema validation without changing `validate_schema(instance, schema)` or `load_project_profile_schema()` behavior.
- [x] Added deterministic `load_named_schema(name)` and `validate_named_schema(instance, name)`.
- [x] Added only the validator features used by the new schemas: type unions, boolean/number/null, pattern, const, maxItems, uniqueItems, typed additional properties, and deterministic issue sorting.
- [x] Added all 11 requested Draft 2020-12 schema files with strict `additionalProperties: false` except explicit free-form maps.
- [x] Added DAG cycle/unknown-edge/acceptance-coverage semantics and optimization forbidden-target semantics.
- [x] Added focused tests using temporary stores and fake KnowledgeSync only.
- [x] Preserved all Tasks 1-6 regression behavior.
- [x] Did not implement Task 8-10 runtime execution, release automation, or optimization application.

## TDD / RED Evidence

The TDD and writing-good-tests instructions were read before editing tests. Each test names an observable contract break and exercises the real validator/protocol/store; only the external KnowledgeSync boundary is faked.

1. Initial clean RED after correcting a test import path:
   - Command: `python3 -m unittest scripts.tests.test_schema_validation scripts.tests.test_phase_protocol`
   - Result: `Ran 16 tests`; `FAILED (failures=16)`, 0 errors.
   - Cause: named-schema interfaces and PhaseProtocol did not exist.
2. Orchestrator construction RED:
   - Command: `python3 -m unittest scripts.tests.test_phase_protocol.OrchestratorPhaseProtocolTests`
   - Result: 1 expected failure: `orchestrator PhaseProtocol construction is not implemented`.
3. Issued-action/profile freshness RED:
   - Command: two named protocol tests.
   - Result: 2 expected failures: forged action returned `SCHEMA_INVALID`; changed profile returned `OK`.
4. Dependency exception RED:
   - Command: named completion exception test.
   - Result: 1 expected failure showing raw `RuntimeError` escaped.
5. Full child mapping RED:
   - Command: named mapping test.
   - Result: 1 expected failure: DIAGNOSE had a null predecessor.
6. Phase-index integrity RED:
   - Command: `python3 -m unittest scripts.tests.test_phase_protocol.PhaseArtifactIndexIntegrityTests`
   - Result: 1 expected failure: a tampered indexed phase was still marked valid.

Two preliminary runs produced test-harness import/AttributeError errors. They were corrected before counting RED evidence, then rerun as assertion failures caused only by missing behavior.

## Implementation Decisions

- Action identity is derived from canonical immutable inputs and persisted under the current source event. Recomputing a changed action under the same event/task identity yields `ACTION_CONFLICT`; a caller-mutated issued action yields `ACTION_ID_MISMATCH`.
- The requirement snapshot is the only null-parent artifact. DIAGNOSE dynamically binds the most recent valid REVIEW/IPIPE/IMPLEMENT failure predecessor.
- PLAN selects the first task-DAG node without a persisted task-plan, preserving task-specific frontier identity.
- `ArtifactStore.put_envelope()` writes through the existing immutable artifact store, then atomically inserts its phase index row. Retry reuses identical rows and conflicts on changed identities. Orphaned immutable content after an interrupted index attempt is harmless and recovery-safe.
- Published markdown is canonical envelope JSON excluding derived `content_hash`, so the KnowledgeSync content hash is independently recomputable.
- Existing KnowledgeSync owns KU document/index/iCafe idempotency and receipts. PhaseProtocol verifies artifact hash, child doc ID/version evidence, and the exact iCafe card/comment evidence before checkpointing.
- Public protocol methods catch dependency/storage exceptions and return stable generic reason codes; validation-policy `ValueError` reason codes are preserved when stable.
- Controller actions enumerate only their allowed side effects. The protocol does not execute child Skills, project builds/tests, iCode, iPipe, or release commands.

## Files

Created:

- `scripts/phase_protocol.py`
- `scripts/tests/test_phase_protocol.py`
- `scripts/tests/test_schema_validation.py`
- `schemas/requirement-snapshot.schema.json`
- `schemas/decision-log.schema.json`
- `schemas/spec.schema.json`
- `schemas/task-dag.schema.json`
- `schemas/task-plan.schema.json`
- `schemas/change-set.schema.json`
- `schemas/review.schema.json`
- `schemas/diagnosis.schema.json`
- `schemas/ipipe-evidence.schema.json`
- `schemas/run-summary.schema.json`
- `schemas/optimization-proposal.schema.json`

Modified:

- `scripts/schema_validator.py`
- `scripts/artifact_store.py`
- `scripts/orchestrator.py`

## Verification

Final exact commands and results:

- `python3 -m unittest scripts.tests.test_schema_validation scripts.tests.test_phase_protocol`
  - `Ran 22 tests in 0.209s` / `OK`
- `python3 -m unittest scripts.tests.test_orchestrator scripts.tests.test_state_and_artifacts scripts.tests.test_knowledge_sync scripts.tests.test_gates`
  - `Ran 73 tests in 3.147s` / `OK`
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`
  - `Ran 297 tests in 16.809s` / `OK`
- `python3 -m py_compile scripts/phase_protocol.py scripts/schema_validator.py scripts/artifact_store.py scripts/orchestrator.py scripts/tests/test_phase_protocol.py scripts/tests/test_schema_validation.py`
  - exit 0, no output
- `python3 -m compileall -q scripts`
  - exit 0, no output
- `python3 -m json.tool` for each of the 11 new schema files
  - all exit 0

## Safety Scans

- Comate-only / no source-material runtime dependency:
  - `rg -n -i 'codex|tom-autorelease' <all Task 7 changed files>`
  - exit 1, zero matches (expected no-match status).
- No local project execution APIs or forbidden command launch strings in affected production files:
  - `rg -n 'subprocess|os\.system|Popen|check_call|check_output|docker[[:space:]]|cmake[[:space:]]|ctest[[:space:]]|ncs[[:space:]]|simulator[[:space:]]' scripts/phase_protocol.py scripts/schema_validator.py scripts/artifact_store.py scripts/orchestrator.py`
  - exit 1, zero matches.
- No direct KU/iCafe/iCode/iPipe client/runtime dependency in the new protocol/schema/artifact boundary:
  - `rg -n 'from clients|import clients|KuClient|CafeClient|IpipeRuntime|IcodeRuntime|requests\.|urllib\.request' scripts/phase_protocol.py scripts/schema_validator.py scripts/artifact_store.py`
  - exit 1, zero matches.
- No live iCafe, KU, Infoflow, iCode, or iPipe call was made. Tests use `FakeKnowledgeSync`, temporary SQLite databases, and temporary artifact roots.
- No BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator, or release command was executed on the Mac.
- `/Users/tom/Desktop/skills` was not initialized as Git and no commit was fabricated.

## SHA-256

```text
84a1d473770ca53ca12c120ea4227bf23b99e87dcc8df88cf164ad7ce0f89b1b  scripts/phase_protocol.py
ff4fec587c9ae14ebd4687cdd564085ec56b51cc566f073bf75f34d333ff3e20  scripts/schema_validator.py
a06617517e525171044a8b6a4aab2037f3761a717a024d3b6ac2cc3a6372dd5d  scripts/artifact_store.py
b35e3ac413ae4fceb0c99924dbe9d9f7a2728defe48fbb682aa3677a6aa15af1  scripts/orchestrator.py
d705d03206ad1a1a7b3200e938aff86e1235aab6e7c674a62ad67d07839d9fff  scripts/tests/test_phase_protocol.py
36f2463af7b65f3f0202dd6d81a3c8345a597574b45f2848ba4f85e899c11cb3  scripts/tests/test_schema_validation.py
f9d31b6f4d05cb641adb87a24d99b7f77bd5b4fba3a73a8df41970035bed0a05  schemas/requirement-snapshot.schema.json
6edddc2b53c9d65d34ba26e8ba130a1a9a4ab27044cd0d115a67dcfd75b8441d  schemas/decision-log.schema.json
d7c73394f8c0fd73738a5dcd1106cc89fdada9920dd1a26cc8e49e15f5b1342a  schemas/spec.schema.json
c935d48aaefbebc36e7ce4e2b88b540ab289be10fe661f4d1645cc48f8941810  schemas/task-dag.schema.json
82a22918b6cad05e41c20d527b8dfb139b72177e2e72d48e37c21e93a81b960c  schemas/task-plan.schema.json
ae120b0f744589452fbaca88885026e464d6ff20bf517b124596db7c1395e7da  schemas/change-set.schema.json
efaa499fd168350906148997c11f23281f5cf9c815e0366965cb6c373c608fdf  schemas/review.schema.json
89ed67c953afc516f78a9e4f6807a15a661659082a599852885b4f2d5e538e9e  schemas/diagnosis.schema.json
2834182430b22875b3fa0cc7eb28a7e77620ade9c4a1edc5fb96564f1728a41f  schemas/ipipe-evidence.schema.json
6ba948d435532af8facee961c4de327affdf8929e38e182a4be5f7b7e0c74554  schemas/run-summary.schema.json
03d9657ceb48c6dabae6fa2edab874f0b3968a99e46f053ad22f3d3910f79d5d  schemas/optimization-proposal.schema.json
```

## Self-Review

- Mutation checks: wrong child mapping, null/nonmatching parent, changed action fields, stale profile/event, empty/mismatched revision set, invalid schema path, wrong content hash, forged approval, secret/noncanonical evidence, wrong KU hash/doc/version evidence, missing iCafe comment evidence, duplicate completion, dependency exception, DAG cycle, forbidden optimization target, and phase-index tampering are each caught by at least one test.
- Existing ArtifactStore `put/get` semantics and project-profile schema validation remain intact; affected and full regression suites are green.
- Persistence contains canonical envelopes, hashes, IDs, and evidence refs only. Test sentinel coverage from prior tasks remains green.
- The implementation does not import or execute source-material release code and introduces no third-party runtime dependency.

## Concerns / Residual Boundaries

- Controller-owned IPIPE and RELEASE outcomes are deliberately not executed by PhaseProtocol. Their existing runtimes remain responsible for dynamic pass/fail routing and environment/release policy; this task only returns bounded controller actions.
- The envelope binds a canonical `knowledge_url` before publication, while existing KnowledgeSync receipts expose doc ID/version/evidence but not the URL. The protocol therefore validates the URL canonically before publication and verifies exact doc ID/version evidence afterward. If a future KnowledgeSync receipt adds URL, it should be matched directly as an additive hardening change.
- Crash recovery intentionally returns `RECOVERY_REQUIRED` while any external intent is unresolved. Reconciliation remains owned by the existing StateStore/KnowledgeSync recovery path; PhaseProtocol never guesses success or duplicates a remote write.

## Task 7 fix round 1

Date: 2026-08-11

### Status

COMPLETE. All blocking Critical findings C1-C7 and Important findings I1-I8 from the independent review are closed under the Repair Rulings. Minor M1 is explicitly deferred. No live external call or local BGW/XFlow compile, test, Docker, NCS, simulator, or release command was executed.

### Finding disposition

- C1: Replaced the invented snapshot schema with the established `RequirementSnapshot` shape and recompute `content_hash` through `requirement_snapshot.validation_error()`. Orchestrator INTAKE persists the real snapshot.
- C2: INTAKE is controller-owned and G0 binds snapshot/project/collaboration prerequisites. Non-G0 approvals bind action/task/parent/revisions plus validated content hash. G5 binds the complete Change Set; REVIEW has no G7; SUBMIT/IPIPE retain G7 ownership.
- C3: Phase results are pre-publication drafts with null remote identity. `complete()` publishes canonical specialized content, verifies generated KU URL/doc/version and iCafe receipt, then stores the enriched final envelope. `KnowledgeSync` response and durable receipt include `child_url`.
- C4: `StateStore.commit_transition_result()` uses `BEGIN IMMEDIATE` to compare the source event and atomically append the transition plus definite idempotency result. Post-publish source/profile/predecessor/artifact/approval checks and cached recovery cover response loss and races.
- C5: The DAG frontier selects only nodes whose dependencies have complete passing REVIEW artifacts. Passing review routes to WORKSPACE for the next task or SUBMIT only after all nodes pass.
- C6: REVIEW ACCEPT/REJECT/INCOMPLETE and blocking findings route to WORKSPACE/SUBMIT, DIAGNOSE, or STOPPED with `REVIEW_INCOMPLETE` as required.
- C7: WORKSPACE/SUBMIT/IPIPE/RELEASE are schema-less controller descriptors and reject `validate_result()`/`complete()`. Explicit iPipe evidence ingestion binds a real submission and dynamic SUCCESS/FAILURE/BLOCKED route. RELEASE no longer declares run-summary.
- I1: Duplicate completion hashes the complete draft, conflicts on change, and revalidates final artifact, receipt, approval, issued action, and committed checkpoint before replay.
- I2: Spec traceability is checked against the root snapshot and internal behavior/scenario IDs. DAG coverage is checked against predecessor Spec acceptance IDs, with duplicate task and coverage ownership validation.
- I3: Task Plan requires exact business/test roles, ordered checklist, DAG test/fixture/acceptance identity, exact revisions, and `g4_input_hash == action.input_hash`. Change Set validates plan baselines, exact revisions/tests, recomputed full diff hash, and recomputed candidate hash.
- I4: Review candidate/baseline matches the predecessor Change Set; Diagnosis frozen revisions and insufficient-evidence route are consistent; iPipe revisions/environment/submission, stage/job identities/statuses, failure signature, and release evidence are consistent.
- I5: Decision-log completion/frontier and all substantive Spec sections are nonempty and consistent. Run-summary is not used by RELEASE and remains Task 8 post-terminal scope under Repair Ruling 5.
- I6: Artifact row and phase index insert share one SQLite transaction. Final exact envelope, named schema, evidence refs, and content hash are revalidated both before indexing and on load.
- I7: Definite dependency reasons use an explicit allowlist; arbitrary uppercase exception messages become stable generic protocol failures.
- I8: Replaced stale fixtures with production-shaped generated KU identity/URL receipts. Added multi-node, rejected/incomplete review, changed replay, corruption, atomic response-loss, publish-crash, concurrent completion, post-publish mutation, predecessor-binding, iPipe controller, diagnosis-incomplete, and durable URL receipt tests.

### TDD evidence

- Publication/approval repair RED: `Ran 5 tests`, `FAILED (failures=5)`; GREEN: `Ran 5 tests`, `OK`.
- Atomicity/recovery repair RED: `Ran 14 tests`, `FAILED (failures=10)`; GREEN: `Ran 14 tests`, `OK`.
- Frontier/controller/semantic slice after the first production patch: `Ran 11 tests`, `FAILED (failures=2, errors=3)`; GREEN: `Ran 11 tests`, `OK`.
- Predecessor/hash slice RED: `Ran 15 tests`, `FAILED (failures=10)`; all ten new assertions became GREEN, followed by the combined schema/repair suite at `Ran 50 tests`, `OK`.
- Recovery integration slice RED: `Ran 4 tests`, `FAILED (failures=2)`; GREEN: `Ran 4 tests`, `OK`.
- iPipe/diagnosis slice RED: `Ran 4 tests`, `FAILED (failures=4)`; GREEN including the existing success/failure route test: `Ran 5 tests`, `OK`.
- Legacy Task 7 contract migration before update: `Ran 17 tests`, `FAILED (failures=13, errors=4)`; after updating rather than deleting coverage: `Ran 17 tests`, `OK`.

### Final verification

- `python3 -m unittest scripts.tests.test_phase_protocol scripts.tests.test_phase_protocol_repair scripts.tests.test_schema_validation scripts.tests.test_state_and_artifacts scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator scripts.tests.test_gates scripts.tests.test_repair_policy scripts.tests.test_runtime_contracts`
  - `Ran 165 tests in 3.959s` / `OK`
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`
  - `Ran 353 tests in 17.136s` / `OK`
- `python3 -m py_compile` for affected production and test modules, then `python3 -m compileall -q scripts`
  - exit 0, no output
- `python3 -m json.tool` for all 11 specialized schema files
  - all exit 0
- Runtime dependency scan for `tom-autorelease`, Codex/Claude metadata/imports, and prohibited local project/release command strings in affected production/schema files
  - zero matches

### SHA-256

```text
92db14effd7df2007e0ea6a0d761208559d540078ce7779bb89af5f61cb94f14  scripts/phase_protocol.py
3e4c6a9c92766f78206c2af84e162337eae2c6b074ebfe5d9e887e088ce74d25  scripts/schema_validator.py
3972341823767f54569dc7820dddf391e2593cb7492d1346f3a13b8cdd4b9245  scripts/artifact_store.py
b7b0df6ba6f7fa90844cebfe89bc6a336f0b8d68e559da4ae4b6dddd2fd62cb9  scripts/state_store.py
40be55bcfbfef3d3c442fe7b862778cd9d5d3c1d7d8c8eef4617966dfb29e03c  scripts/knowledge_sync.py
4775a3ecd902a6c02f22e0f7bee6c0a9e61c084068123a42ceaf0fe00ded9afd  scripts/transition_policy.py
d33e8dd1cf38daca1995d42fe3a91c867876bc0f2589d4f1674dbf6cc7ed9166  scripts/evidence_policy.py
73325bea4bac9a9b5bbffbc0d75a21b94f00314fb39328997c1710af0dd0c6d0  scripts/orchestrator.py
70d5e4d94c556ada35cbb04441c42481840ad5dad298db393fd34b6b49ad7190  scripts/tests/test_phase_protocol.py
d785a2b16d2a6b7a3b7463e7d73dd23a60d0725b5875d74ea70da92b639579a6  scripts/tests/test_phase_protocol_repair.py
7c9825224d8b25f9f6d81a9d39a14b82fe1a301dee2621900754350eb335e265  scripts/tests/test_schema_validation.py
8098436103c94ef4c79847c1546e7a5cfe4a643550110487b57e75cc4c7bdf0b  scripts/tests/test_knowledge_sync.py
33a7579c9eedcc456f1951b2881acb2f3b44d4dee0ca0f518946368db24c63a1  schemas/requirement-snapshot.schema.json
6edddc2b53c9d65d34ba26e8ba130a1a9a4ab27044cd0d115a67dcfd75b8441d  schemas/decision-log.schema.json
d7c73394f8c0fd73738a5dcd1106cc89fdada9920dd1a26cc8e49e15f5b1342a  schemas/spec.schema.json
c935d48aaefbebc36e7ce4e2b88b540ab289be10fe661f4d1645cc48f8941810  schemas/task-dag.schema.json
82a22918b6cad05e41c20d527b8dfb139b72177e2e72d48e37c21e93a81b960c  schemas/task-plan.schema.json
ae120b0f744589452fbaca88885026e464d6ff20bf517b124596db7c1395e7da  schemas/change-set.schema.json
efaa499fd168350906148997c11f23281f5cf9c815e0366965cb6c373c608fdf  schemas/review.schema.json
59ad341740ca7d76d1ea18075e6f1ea16960289b0af4599d03a2e94cfb26dd3d  schemas/diagnosis.schema.json
6be319bffa373d7a5c10ab2c9587a40d512e112b0b24342c312cd2c290acf2d3  schemas/ipipe-evidence.schema.json
6ba948d435532af8facee961c4de327affdf8929e38e182a4be5f7b7e0c74554  schemas/run-summary.schema.json
03d9657ceb48c6dabae6fa2edab874f0b3968a99e46f053ad22f3d3910f79d5d  schemas/optimization-proposal.schema.json
```

### Concerns

- M1 remains deferred exactly as ruled: repeated diagnosis KU titles still use attempt 1.
- Run-summary production and durable-event binding remain Task 8 post-terminal work and are intentionally absent from RELEASE.
- The workspace is not Git; no repository was initialized and no commit was fabricated.

## Task 7 fix round 2

Date: 2026-08-11

### Status

COMPLETE. All scoped round-1 re-review findings are addressed. The repair was developed with clean focused RED tests, fake transports, and temporary state/artifact/profile directories only. No live external call, project compile/test, Docker, NCS, simulator, release command, Git initialization, or commit was performed.

### Finding disposition

- C2: `Orchestrator.start()` now requires and validates a real iCafe snapshot before creating a run. The durable INTAKE event binds the run, project, card ID/title/content hash, pinned profile, canonical group name, owner, exact roles and member snapshot, `friendlyLevel`, and the durable `infoflow.group.create:<run_id>` session key. The G0 input hash covers the complete prerequisite object. `next(INTAKE)` and completion fail closed on missing, malformed, unapproved, or receipt-mismatched collaboration state without predicting a group ID.
- I1: Definite phase and iPipe replay loads the exact `artifact_id` stored in the original result through `ArtifactStore.phase_artifact()`. A newer artifact for the same run/phase/task cannot redirect historical replay; corruption of the exact historical artifact still fails integrity validation.
- I2: Production acceptance values are normalized by one shared validator. String IDs and object IDs are accepted, while empty, malformed, and duplicate normalized IDs are rejected deterministically. Snapshot validation, named-schema validation, and Spec predecessor traceability all use that same contract; production-shaped `acceptance: ["AC-1"]` fixtures cover success, omission, and unknown IDs.
- I3: IMPLEMENT actions expose and hash `baseline_revisions`, while accepted Change Sets provide candidate `source_revisions` equal to `content.revisions`. G5 and the completion checkpoint bind candidates, so REVIEW inherits the generated revisions. A natural PLAN `r1/t1` to IMPLEMENT `r2/t2` flow succeeds and undeclared candidate identities fail.
- I4: iPipe actions, ingestion, post-publication checks, and cached replay bind the pinned profile bytes plus the profile-owned pipeline ID, repository module, release rule, environment fingerprint, source revisions, and exact submission artifact `controller_binding`. Job and remote evidence references pass through the canonical non-secret persistence policy. Mismatches and profile drift fail closed.
- I5: Decision-log semantics now enforce all three result states. `DECISIONS_RECORDED` requires coherent nonempty completed decisions and an empty frontier; `NO_OPEN_DECISIONS` requires no decisions/frontier and completion; `UNRESOLVED` requires an incomplete status and nonempty frontier. Decision IDs and options are unique, and each choice must be declared in its options.
- I8: Focused production-shaped tests cover strict start/G0 prerequisites and durable group receipt, string acceptance traceability, natural baseline-to-candidate revision flow, owned iPipe binding and nested evidence rejection, exact old replay after re-entry and exact-artifact corruption, decision semantics, final evidence preservation, next-action propagation, and cached pinned-profile drift.
- N1: Final phase and iPipe envelopes use a stable deduplicating evidence union: validated draft/source references first in original order, followed by canonical KU/index/iCafe receipt references. The preserved union is revalidated and propagates to the next action.

### TDD evidence

- Acceptance/revision group (four named focused selectors) RED: `Ran 4 tests`, `FAILED (failures=10)`; GREEN: `Ran 5 tests`, `OK` after adding the strict start coverage.
- Strict start selector RED: `Ran 1 test`, `FAILED (failures=1)`; included in the Group A GREEN result.
- Exact historical replay group (phase and iPipe selectors) RED: `Ran 2 tests`, `FAILED (failures=2)`; GREEN: `Ran 2 tests`, `OK`.
- iPipe ownership, decision semantics, and phase evidence-union group RED: `Ran 4 tests`, `FAILED (failures=10)`; GREEN: `Ran 4 tests`, `OK`.
- G0 intake/session group clean RED after excluding one test-fixture setup defect: `Ran 3 tests`, `FAILED (failures=10)`; GREEN: `Ran 3 tests`, `OK`.
- Additional production iPipe evidence-union selector: `Ran 1 test`, `OK`.
- Final audit regression for unrelated pinned-profile byte drift RED: `Ran 1 test`, `FAILED (failures=1)`; GREEN after the shared binding fix: `Ran 1 test`, `OK`.

The RED assertions failed on missing production behavior rather than import, syntax, fixture, or dependency errors. The one excluded Group D run failed while constructing its fixture and is not counted as RED evidence.

### Final verification

- `python3 -m unittest scripts.tests.test_phase_protocol scripts.tests.test_phase_protocol_repair scripts.tests.test_schema_validation scripts.tests.test_state_and_artifacts scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator scripts.tests.test_gates scripts.tests.test_repair_policy scripts.tests.test_runtime_contracts scripts.tests.test_task5_safety scripts.tests.test_collaboration`
  - `Ran 222 tests in 4.931s` / `OK`
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`
  - `Ran 380 tests in 19.570s` / `OK`
- `python3 -m py_compile scripts/collaboration.py scripts/orchestrator.py scripts/phase_protocol.py scripts/artifact_store.py scripts/requirement_snapshot.py scripts/schema_validator.py scripts/tests/test_phase_protocol_repair.py scripts/tests/test_schema_validation.py scripts/tests/test_task5_safety.py scripts/tests/test_orchestrator.py scripts/tests/test_phase_protocol.py`
  - exit 0, no output
- `python3 -m compileall -q scripts`
  - exit 0, no output
- `python3 -m json.tool` for all 11 specialized schema files
  - all exit 0
- Case-insensitive runtime dependency scan for Codex, Claude, and `tom-autorelease` across affected production/schema files
  - zero matches (expected `rg` exit 1)
- Prohibited local project/release command API and launch-string scan across affected production files
  - zero matches (expected `rg` exit 1)
- Direct KU/iCafe/iCode/iPipe client/runtime import scan across the repaired protocol boundary
  - zero matches (expected `rg` exit 1)

### SHA-256

```text
2afa352b24729ee52645c6bf565a67c90edad2b75e104a5c423e35e87d8eb8e3  scripts/collaboration.py
5ab2e0b3b287aefe59b3d13607e01b20e8156b79dc4a7950167fec080bc9c8ee  scripts/orchestrator.py
af0b386d9307069c6121fb20082c5485a4282a1387249b86a915c3dc90d75b6c  scripts/phase_protocol.py
9df632c76cddf38764bd7c2faa28d45dfb35dc7991453d721d74c33692df139c  scripts/artifact_store.py
33fe510a7d142c6e7fa6b5f88420572fd0435e003ef465bca2aa5259f493310b  scripts/requirement_snapshot.py
8b37589dc9706be304cb2e7a213dba9481f097ffa3101af62845cb7417f45290  scripts/schema_validator.py
09b451d0b1adf0a48af95ea85a4cfbf6735ab078572cbdfac2b327c18c0e0ff1  scripts/tests/test_phase_protocol_repair.py
c0ea37300ef9e95f577816224b6e10c1d1e7a410165e977518fb87efda2fc1c9  scripts/tests/test_schema_validation.py
1b993321bfd42243fd59e4f1a5bbca1f678b765f3bf316336dcd218483b07a7d  scripts/tests/test_task5_safety.py
e26a09eecf13fa2685da7f8bbd06f5cefee62bd3a06587d01ecd4231b717a5b3  scripts/tests/test_orchestrator.py
a4d8a4d5027b240f34407f60952bb2c6664b62928edf4e3bf22475bd8ad645db  scripts/tests/test_phase_protocol.py
```

### Concerns

- No unresolved concern remains within this repair scope.
- The workspace is not a Git repository; file hashes and executable verification output are the reproducibility evidence.

## Task 7 fix round 3

Date: 2026-08-11

### Status

COMPLETE. Blocking findings I4, I8, and N2 from the round-2 re-review are addressed without weakening strict snapshot, pinned-profile, submission, approval, or iPipe evidence validation. The implementation and new focused tests use only local fake boundaries and temporary state/artifact/profile paths. No live external write or local BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator, or release operation was executed.

### Finding disposition

- I4: Added the production `Orchestrator.submit_to_ipipe()` controller path. It invokes the run-bound iCode runtime, requires the exact durable `icode.submit` intent/receipt, verifies the complete normalized intent and G7 candidate hash, reloads the pinned project profile, derives owned pipeline/module/release/environment/revision values, stores the canonical iCode receipt as the submission artifact with exact `metadata.controller_binding`, and atomically commits the top-level `SUBMIT -> IPIPE` payload consumed by `PhaseProtocol.next()` and `ingest_ipipe_evidence()`. Caller data cannot select pipeline identity, release policy, environment fingerprint, or source revisions.
- I8: Replaced the round-2 synthetic `ipipe_case()` and direct `StateStore.transition(..., "IPIPE", ...)` tests with `test_task7_controller_boundary.py`. The fixture starts through strict `Orchestrator.start()`, reaches SUBMIT through public orchestrator transitions, uses a durable fake iCode transport boundary, and obtains every IPIPE event and predecessor artifact through `submit_to_ipipe()`. Production-bound tests cover success, pipeline/module/release/revision/environment mismatch, submission corruption, G7 mismatch, nested secret-bearing evidence rejection, exact historical replay and corruption, pinned-profile drift, and evidence preservation/next-action propagation.
- N2: CLI `start` now captures the canonical requirement snapshot with the established `CafeClient.snapshot()` adapter and passes it to strict `Orchestrator.start()`. Adapter failures are returned as non-ready structured results. Centralized CLI exit evaluation returns nonzero for every `ready: false`, `ok: false`, run-not-found, or non-success reason. Focused CLI tests execute the real parser/main boundary with an injected fake Cafe adapter and prove valid start plus missing and invalid snapshot failures.

### Controller contract

- The submission artifact content is canonical JSON of the verified durable iCode receipt; its metadata contains the exact controller binding and durable intent identity.
- The IPIPE event carries profile path/hash, project/card identity, pipeline ID, business module, release rule, normalized business/test revisions, environment fingerprint, submission ID/hash, and G7 ID/input hash at top level.
- The source SUBMIT event and definite controller result are committed with one event CAS. A concurrent source change returns `STALE_ACTION`; a result identity conflict returns `SUBMISSION_CONFLICT`.
- Real iCode path normalization is mirrored exactly when comparing the durable intent. Unexpected runtime exceptions become stable `SUBMISSION_BINDING_FAILED` results and do not change state.

### TDD evidence

- Initial boundary run was excluded because approval delivery setup and one assertion shape produced two fixture errors: `Ran 3 tests`, `FAILED (failures=2, errors=2)`.
- Clean controller/CLI RED: `python3 -m unittest scripts.tests.test_task7_controller_boundary` -> `Ran 3 tests`, `FAILED (failures=4)`. Observable failures were missing `submit_to_ipipe`, CLI returning `ICAFE_SNAPSHOT_REQUIRED` without adapter capture, and CLI failures returning exit 0.
- Minimal controller/CLI GREEN: the same command -> `Ran 3 tests`, `OK`.
- First extended production-boundary run was excluded because the later-history fixture reused the old action identity and raised `ARTIFACT_CONFLICT`: `Ran 9 tests`, `FAILED (errors=1)`. After assigning a distinct later action/source identity: `Ran 9 tests`, `OK`.
- Real `IcodeRuntime` normalized-path regression RED: one named controller test -> `Ran 1 test`, `FAILED (failures=1)` with stable `ICODE_RECEIPT_MISMATCH`; GREEN after canonical expected-intent comparison: full boundary suite `Ran 9 tests`, `OK`.
- Runtime-exception boundary RED: one named controller test -> `Ran 1 test`, `FAILED (failures=1)` because `RuntimeError` escaped; GREEN after the stable public exception boundary: full boundary suite `Ran 10 tests`, `OK`.

Excluded runs failed in test setup rather than at the intended missing production behavior and are not counted as RED evidence.

### Final verification

- `python3 -m unittest scripts.tests.test_task7_controller_boundary scripts.tests.test_phase_protocol scripts.tests.test_phase_protocol_repair scripts.tests.test_schema_validation scripts.tests.test_state_and_artifacts scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator scripts.tests.test_gates scripts.tests.test_repair_policy scripts.tests.test_runtime_contracts scripts.tests.test_task5_safety scripts.tests.test_collaboration scripts.tests.test_adapters scripts.tests.test_icode_runtime scripts.tests.test_ipipe_runtime`
  - `Ran 313 tests in 9.982s` / `OK`
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`
  - `Ran 376 tests in 19.965s` / `OK`
- `python3 -m py_compile scripts/orchestrator.py scripts/phase_protocol.py scripts/artifact_store.py scripts/schema_validator.py scripts/requirement_snapshot.py scripts/collaboration.py scripts/clients/icafe_client.py scripts/clients/icode_runtime.py scripts/tests/test_task7_controller_boundary.py scripts/tests/test_phase_protocol_repair.py`
  - exit 0, no output
- `python3 -m compileall -q scripts`
  - exit 0, no output
- `python3 -m json.tool` for all 11 specialized schema files
  - all exit 0

### Safety verification

- Case-insensitive scan for Codex, Claude, and `tom-autorelease` across affected production/test files
  - zero matches (expected `rg` exit 1)
- Scan for local project compile/test/Docker/NCS/simulator/release launch strings across affected production files
  - zero matches (expected `rg` exit 1)
- Scan for `ipipe_case()` or direct `IPIPE` state transition in the repaired/new controller tests
  - zero matches (expected `rg` exit 1)
- Scan for subprocess, Git initialization/commit, and prohibited project commands in the new focused test module
  - zero matches (expected `rg` exit 1)
- Production uses only the established local `clients.icafe_client.CafeClient` and caller-supplied run-bound `IcodeRuntime` boundary; no new third-party runtime dependency was added.
- The workspace was not initialized as Git and no commit was fabricated.

### SHA-256

```text
2ed1d2f4cb875a64ac971d5a88017c269e97fcd30bb33e95e9e227edde32d954  scripts/orchestrator.py
e90f2d741b7da2f14dd02c5518c803ba0e8e038be334ad53933e937ddcfe6d38  scripts/tests/test_phase_protocol_repair.py
4cf5d02cbcdd8d8617657a11922233d28ada8392ddbdff35df8b43aefcb9e1f5  scripts/tests/test_task7_controller_boundary.py
```

### Concerns

- No unresolved concern remains within the round-3 repair scope.
- The real adapters were not called during verification; production ownership is established through durable fake-boundary evidence only.
