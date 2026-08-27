# Executable Tom Autodev Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task with a verification checkpoint after each task.

**Goal:** Make `tom-autodev` a Comate-only, fail-closed delivery controller that reads iCafe requirements, archives every phase in BGW/XFlow knowledge bases, coordinates development/test collaboration in 如流, generates source and product-test changes, invokes local Review, submits iCode, monitors iPipe, routes failures, and proposes guarded G10 Skill improvements.

**Architecture:** Keep the deterministic control plane in Python. The Comate host executes phase Skills through a structured `PhaseProtocol`; platform adapters invoke real CLIs and embedded components. Migrate the required iCode/iPipe/Infoflow/retry/release behavior from `tom-autorelease` into `tom-autodev` and remove runtime dependency on the old Skill.

**Tech Stack:** Python 3.11+ standard library, SQLite, JSON Schema files, YAML profiles, `icafe-cli`, `ku`, `icode-cli`, embedded Infoflow gateway/scripts, iPipe HTTP client, unittest.

## Global Constraints

- Comate is the only supported host; do not add Codex compatibility or a second entrypoint.
- `tom-autorelease` is source material only; production code must not import or execute it.
- Mac may inspect source, generate source/tests, manage worktrees, run source Review, and parse remote evidence only.
- Never run BGW/XFlow compilation, unit, regression, integration, Docker, NCS, simulator, or local substitute commands.
- All state transitions and external side effects are fail-closed and idempotent.
- iCafe requirement正文 is an immutable snapshot; phase links are added as comments and configured status transitions.
- BGW KU target is repo `sX0BTOBWJX`, parent `I15ClP2KW4ZGAK`; XFlow target is the same repo, parent `meQ-Acjg0K09Xr`.
- Every phase artifact is versioned, content-hashed, schema-validated, published to KU, and linked back to iCafe.
- G0-G10 approvals bind canonical input hashes; G10 is required before applying any Skill/control-plane optimization.
- Tests added to this repository are control-plane, adapter-contract, and fake-E2E tests only.

## File Map

### Existing files to modify

- `tom-autodev/scripts/evidence_gate.py`: replace permissive comparisons with state-specific fail-closed evidence policies.
- `tom-autodev/scripts/orchestrator.py`: integrate policies, protocol, adapters, collaboration, persistence, and complete CLI commands.
- `tom-autodev/scripts/project_registry.py`: use the profile JSON Schema and deep capability/path validation; unify the config path.
- `tom-autodev/scripts/workspace_manager.py`: create and own isolated worktrees, locks, and heartbeats.
- `tom-autodev/scripts/state_store.py`: persist run checkpoints, intents, receipts, artifacts, locks, and handoffs atomically.
- `tom-autodev/scripts/artifact_store.py`: add artifact metadata lookup and schema/evidence indexing.
- `tom-autodev/scripts/approval_ledger.py`: add gate validation, expiry, timeout, channel membership, late-response audit, and run binding.
- `tom-autodev/scripts/clients/icafe_client.py`: replace injected-only contract with a safe CLI transport and card/comment/status operations.
- `tom-autodev/scripts/clients/icode_client.py`: add preflight, CR submission, revision and patchset receipt handling.
- `tom-autodev/scripts/clients/ipipe_client.py`: add pipeline discovery, trigger, status, stage/job evidence, rerun, and release verification.
- `tom-autodev/scripts/tests/*.py`: extend control-plane and adapter-contract coverage.
- `tom-autodev/SKILL.md` and phase/project/language Skills: document the executable protocol, Comate-only boundary, embedded runtime, and phase contracts.

### New files

- `tom-autodev/scripts/evidence_policy.py`: state-to-evidence and state-to-approval requirements.
- `tom-autodev/scripts/transition_policy.py`: legal transitions, failure routes, and terminal-state checks.
- `tom-autodev/scripts/phase_protocol.py`: `next-action`, phase result envelope, and deterministic handoff validation.
- `tom-autodev/scripts/knowledge_sync.py`: KU create/query/edit/publish and iCafe comment/index synchronization.
- `tom-autodev/scripts/collaboration.py`: `CollaborationSession`, member resolution, role routing, and message receipt handling.
- `tom-autodev/scripts/run_summary.py`: run metrics, failure signatures, and G10 proposal generation.
- `tom-autodev/scripts/cli_transport.py`: subprocess transport with redacted arguments, exit-code mapping, and bounded timeout.
- `tom-autodev/scripts/clients/ku_client.py`: real `ku` CLI adapter.
- `tom-autodev/scripts/clients/infoflow_group_client.py`: group creation, membership, message and @ adapter.
- `tom-autodev/scripts/clients/infoflow_approval_client.py`: embedded approval/wait transport extracted from the old gateway behavior.
- `tom-autodev/scripts/clients/ipipe_runtime.py`: extracted iPipe monitor, retry budget, failure-log and release-evidence helpers.
- `tom-autodev/scripts/clients/icode_runtime.py`: extracted iCode preflight and submission helpers.
- `tom-autodev/scripts/profile_discovery.py`: repository/iCode/iPipe/test-repository candidate discovery.
- `tom-autodev/scripts/schema_validator.py`: local JSON Schema validation with deterministic error paths.
- `tom-autodev/schemas/*.schema.json`: requirement snapshot, decisions, Spec, task DAG, task plan, change set, review, diagnosis, iPipe evidence, run summary and optimization proposal schemas.
- `tom-autodev/scripts/tests/test_phase_protocol.py`, `test_knowledge_sync.py`, `test_collaboration.py`, `test_profile_discovery.py`, `test_fake_e2e.py`.

## Task 1: Close Evidence and Transition Gates

**Files:**
- Create: `tom-autodev/scripts/evidence_policy.py`
- Create: `tom-autodev/scripts/transition_policy.py`
- Modify: `tom-autodev/scripts/evidence_gate.py`
- Modify: `tom-autodev/scripts/orchestrator.py`
- Test: `tom-autodev/scripts/tests/test_gates.py`, `tom-autodev/scripts/tests/test_orchestrator.py`

**Interfaces:**
- `EvidencePolicy.required(action: str, context: dict) -> EvidenceRequirement`
- `EvidenceGate.check(action: str, context: dict) -> dict`
- `TransitionPolicy.validate(current: str, next_state: str, evidence: dict) -> dict`
- `TransitionPolicy.failure_target(current: str, reason_code: str) -> str`

- [ ] **Step 1: Write failing tests for missing evidence.** Add tests proving `{}` cannot advance any non-INTAKE state, missing input/approved hash is blocked, missing approval is blocked, and `None` revisions/environment fingerprints are rejected when the target gate requires them.
- [ ] **Step 2: Run the gate tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_gates.py -v`; expect failures because current `None == None` comparisons pass.
- [ ] **Step 3: Implement explicit per-gate requirements.** Define required artifacts and fields for G0-G10, reject missing values rather than comparing optional keys, validate timestamps, revisions, environment fingerprint and approval ID, and return stable reason codes.
- [ ] **Step 4: Write failing transition tests.** Prove `route_failure()` cannot write a state outside `ALLOWED_TRANSITIONS`, terminal runs cannot advance, and an invalid `next_state` returns a structured block instead of an unguarded insert.
- [ ] **Step 5: Implement transition policy.** Make `advance()` and `route_failure()` call the same policy before `StateStore.transition`; preserve idempotency keys and record policy evidence in the event payload.
- [ ] **Step 6: Run focused tests and the full control-plane suite.** Run `python3 -m unittest discover -s tom-autodev/scripts/tests -v`; expected result is all tests passing with no local project execution.
- [ ] **Step 7: Commit the gate change.** In a future Git checkout, commit `fix: make tom-autodev gates fail closed` after verification. The current `/Users/tom/Desktop/skills` directory has no Git repository, so do not fabricate a commit here.

## Task 2: Deep Project Profiles and Discovery

**Files:**
- Create: `tom-autodev/scripts/profile_discovery.py`, `tom-autodev/scripts/schema_validator.py`
- Modify: `tom-autodev/scripts/project_registry.py`
- Modify: `tom-autodev/schemas/project-profile.schema.json`
- Modify: `setup-tom-autodev/SKILL.md`
- Test: `tom-autodev/scripts/tests/test_project_registry.py`, `tom-autodev/scripts/tests/test_profile_discovery.py`

**Interfaces:**
- `validate_profile(profile: dict, *, check_paths: bool = True) -> dict`
- `discover_candidates(project_root: Path, *, iCode: ..., iPipe: ...) -> dict`
- `save_profile(path: Path, profile: dict, previous_hash: str | None, confirmation: bool) -> dict`

- [ ] **Step 1: Add failing schema and deep-validation tests.** Cover missing nested repository path/module/branch/lock, absent independent test repo, invalid Skill paths, unknown Review provider, empty approval channels, missing pipeline allowlist, incomplete environment profile, invalid KU parent, and non-existent paths.
- [ ] **Step 2: Run profile tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_project_registry.py -v`; current truthy top-level checks will incorrectly return `READY`.
- [ ] **Step 3: Implement JSON Schema validation and semantic checks.** Load `project-profile.schema.json`, validate exact nested fields, resolve Skill paths, inspect Git repositories read-only, verify KU repo/parent IDs and reject secrets. Return all missing/invalid paths in deterministic order.
- [ ] **Step 4: Fix the runtime/setup path.** Make setup and `Orchestrator` both use `~/.tom-autodev/config/projects`; support an explicit `--config-root` override without changing the child directory contract.
- [ ] **Step 5: Implement candidate discovery.** Read Git remotes and revisions, call injected iCode/iPipe discovery clients, identify independent test-repository candidates by configured mapping only, and return `PROFILE_CONFIRMATION_REQUIRED` when more than one candidate exists.
- [ ] **Step 6: Protect profile writes.** Require human confirmation for first save and overwrite; store previous hash and never log secret values.
- [ ] **Step 7: Run focused and full control-plane tests.** Use `python3 -m unittest discover -s tom-autodev/scripts/tests -v`.

## Task 3: Durable State, Artifacts, Worktrees and Recovery

**Files:**
- Modify: `tom-autodev/scripts/state_store.py`, `artifact_store.py`, `approval_ledger.py`, `workspace_manager.py`
- Create: `tom-autodev/scripts/lock_manager.py`, `tom-autodev/scripts/recovery.py`
- Test: `tom-autodev/scripts/tests/test_state_and_artifacts.py`, `test_gates.py`, `test_workspace_recovery.py`

**Interfaces:**
- `StateStore.intent(run_id, operation, idempotency_key, payload) -> dict`
- `StateStore.receipt(intent_id, response, evidence_refs) -> dict`
- `ArtifactStore.get(artifact_id) -> dict | None`
- `LockManager.acquire(key, owner_token, ttl_seconds) -> dict`
- `WorkspaceManager.create(repo_path, run_id, task_id, baseline) -> dict`
- `Recovery.resume(run_id) -> dict`

- [ ] **Step 1: Write failing tests for atomic intent/receipt recovery.** Simulate a process exit after intent and assert resume queries rather than repeats; simulate duplicate receipt and assert idempotent return.
- [ ] **Step 2: Run the recovery tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_workspace_recovery.py -v`.
- [ ] **Step 3: Add SQLite tables and atomic transactions.** Add `intents`, `receipts`, `artifacts`, `locks`, `heartbeats`, `handoffs`, and `external_results`; preserve existing event/idempotency tables.
- [ ] **Step 4: Implement real worktree creation.** Use `git worktree add --detach` or a named task branch from the recorded baseline, return the created path, and never return the source checkout as `worktree_path`.
- [ ] **Step 5: Implement lock and heartbeat ownership.** Use atomic file/SQLite ownership, PID checks, TTL and heartbeat freshness; reject active owners and make stale takeover explicit.
- [ ] **Step 6: Implement checkpoint recovery.** Resume from the last committed state and query each uncertain external intent before allowing a transition.
- [ ] **Step 7: Run all control-plane tests.** Confirm no test invokes a BGW/XFlow command.

## Task 4: Real iCafe, KU and Knowledge Synchronization

**Files:**
- Create: `tom-autodev/scripts/cli_transport.py`, `tom-autodev/scripts/clients/ku_client.py`, `tom-autodev/scripts/knowledge_sync.py`
- Modify: `tom-autodev/scripts/clients/icafe_client.py`, `orchestrator.py`
- Test: `tom-autodev/scripts/tests/test_adapters.py`, `test_knowledge_sync.py`

**Interfaces:**
- `CafeClient.snapshot(card_id: str) -> RequirementSnapshot`
- `CafeClient.comment(card_id: str, content: str, idempotency_key: str) -> Receipt`
- `CafeClient.update_status(card_id: str, status: str, expected_current: str) -> Receipt`
- `KuClient.create_artifact(parent_doc_id: str, title: str, markdown: str) -> ArtifactReceipt`
- `KnowledgeSync.publish_phase(run_id: str, artifact: ArtifactEnvelope) -> KnowledgeReceipt`

- [ ] **Step 1: Write failing fake-transport tests.** Cover iCafe business-code failure, KU create/query/publish failure, command timeout, redacted arguments, duplicate comments, and card status not reachable.
- [ ] **Step 2: Run adapter tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_adapters.py tom-autodev/scripts/tests/test_knowledge_sync.py -v`.
- [ ] **Step 3: Implement bounded CLI transport.** Export required user variables at process launch, never print token values, use argument arrays, enforce timeout, parse JSON and map exit/business codes.
- [ ] **Step 4: Implement iCafe snapshot/comment/status adapter.** Use `card get/query/smart-find`, `comment create`, `next-statuses`, and `card update`; implement close/cancel delete semantics only after approval.
- [ ] **Step 5: Implement KU adapter.** Use `query-content`, `query-version`, `create-doc`, `edit-content`, `publish-doc`; query after every write and persist remote IDs/URLs/hashes.
- [ ] **Step 6: Implement phase synchronization.** Create immutable child document, update/publish root index, write one idempotent iCafe comment, and store both receipts before completion.
- [ ] **Step 7: Run focused and full tests.** Do not execute a live write while running unit tests; live preflight remains a separate acceptance task.

## Task 5: Embedded 如流 Collaboration and Approval

**Files:**
- Create: `tom-autodev/scripts/collaboration.py`, `clients/infoflow_group_client.py`, `clients/infoflow_approval_client.py`
- Create: `tom-autodev/infoflow-gateway/` by extracting the required gateway source and tests from the old implementation without importing it at runtime
- Modify: `tom-autodev/scripts/approval_ledger.py`, `orchestrator.py`
- Test: `tom-autodev/scripts/tests/test_collaboration.py`, `test_approval_channels.py`

**Interfaces:**
- `CollaborationSession.create(run_id, project, card, members) -> CollaborationReceipt`
- `CollaborationSession.route_failure(failure_bundle) -> RoutingDecision`
- `InfoflowApprovalClient.request(request) -> ApprovalRequest`
- `InfoflowApprovalClient.wait(request_id, timeout_seconds) -> ApprovalResponse`

- [ ] **Step 1: Write failing tests for member resolution and role routing.** Cover fixed profile members, iCafe field mapping, unresolved full email, test/environment/code/mixed failures, duplicate messages, and group-create idempotency.
- [ ] **Step 2: Run collaboration tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_collaboration.py -v`.
- [ ] **Step 3: Implement member resolution.** Require complete emails, append approved card owners only when configured, and return `MEMBER_CONFIRMATION_REQUIRED` for unresolved members.
- [ ] **Step 4: Implement group lifecycle.** Run `setup.sh --check`, create/reuse group with `friendlyLevel=3`, record member snapshot and receipts, and make group creation a G0 side effect.
- [ ] **Step 5: Implement role-routed Markdown messages.** Call `msg_send_group.sh` with content and matching `atUsers`; include evidence links and avoid duplicate sends.
- [ ] **Step 6: Extract approval wait behavior.** Move the old gateway’s request/wait/reply/heartbeat behavior into this Skill, remove the runtime path reference to `tom-autorelease`, and keep tokens in existing config only.
- [ ] **Step 7: Add approval channel audit.** Persist delivery receipts, first-valid response, conflicts, timeout and late responses; require Comate and 如流 channel configuration.
- [ ] **Step 8: Run all control-plane tests.** No live group creation occurs in tests.

## Task 6: Extract iCode/iPipe/Release Runtime

**Files:**
- Create: `tom-autodev/scripts/clients/icode_runtime.py`, `tom-autodev/scripts/clients/ipipe_runtime.py`
- Modify: `tom-autodev/scripts/clients/icode_client.py`, `ipipe_client.py`, `orchestrator.py`
- Test: `tom-autodev/scripts/tests/test_adapters.py`, `test_ipipe_runtime.py`

**Interfaces:**
- `IcodeRuntime.preflight(repo_path: Path) -> PreflightReceipt`
- `IcodeRuntime.submit(change_set, approval) -> SubmissionReceipt`
- `IpipeRuntime.discover(profile, revision_set) -> PipelineCandidate`
- `IpipeRuntime.trigger(profile, revision_set, approval) -> BuildReceipt`
- `IpipeRuntime.monitor(build_id, deadline) -> PipelineEvidence`
- `IpipeRuntime.rerun(stage_build_id, approval) -> StageReceipt`
- `IpipeRuntime.verify_release(build_id, revision_set) -> ReleaseEvidence`

- [ ] **Step 1: Write failing contract tests.** Cover iCode login/preflight failure, CR submission idempotency, iPipe allowlist filtering, build association by revision/module, stage failure detection before pipeline failure, bounded retries, and release revision mismatch.
- [ ] **Step 2: Run runtime tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_ipipe_runtime.py -v`.
- [ ] **Step 3: Extract iCode runtime helpers.** Copy only required behavior from the old implementation into focused modules; invoke the real system iCode CLI, never an alias or Git push fallback.
- [ ] **Step 4: Extract iPipe runtime helpers.** Implement the existing API methods needed for discovery, trigger, status, stage/job logs, manual rerun, retry budget and release verification.
- [ ] **Step 5: Persist every external intent/receipt.** Associate iCode revision, CR number, patchset, iPipe build, stage and release evidence with the run and input hash.
- [ ] **Step 6: Integrate G7-G9 transitions.** Require approval before submit, trigger/manual continuation/rerun and release action; route failures into Diagnose and collaboration messages.
- [ ] **Step 7: Run full control-plane tests.** Never trigger a real pipeline during unit/contract tests.

## Task 7: Phase Protocol and Specialized Artifact Schemas

**Files:**
- Create: `tom-autodev/scripts/phase_protocol.py`, `schema_validator.py`
- Create: `tom-autodev/schemas/requirement-snapshot.schema.json`, `decision-log.schema.json`, `spec.schema.json`, `task-dag.schema.json`, `task-plan.schema.json`, `change-set.schema.json`, `review.schema.json`, `diagnosis.schema.json`, `ipipe-evidence.schema.json`, `run-summary.schema.json`, `optimization-proposal.schema.json`
- Modify: `tom-autodev/scripts/orchestrator.py`, `artifact_store.py`
- Test: `tom-autodev/scripts/tests/test_phase_protocol.py`, `test_schema_validation.py`

**Interfaces:**
- `PhaseProtocol.next(run_id: str) -> PhaseAction`
- `PhaseProtocol.validate_result(action: PhaseAction, result: dict) -> ArtifactEnvelope`
- `PhaseProtocol.complete(run_id: str, envelope: ArtifactEnvelope) -> dict`

- [ ] **Step 1: Write failing tests for action and artifact contracts.** Cover phase ordering, task-specific plans, missing parent hash, missing KU receipt, wrong phase schema, stale input, and Comate-only action metadata.
- [ ] **Step 2: Run protocol tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_phase_protocol.py -v`.
- [ ] **Step 3: Implement the artifact envelope and validators.** Validate phase-specific required fields, canonical hashes, source revisions, evidence references and published KU receipt.
- [ ] **Step 4: Implement `next-action`.** Return the exact child Skill name, phase inputs, required human gate, allowed side effects and completion predicate; never return an action that violates current state.
- [ ] **Step 5: Implement phase completion.** Store artifact, publish KU, comment iCafe, resolve approvals and transition atomically from the last checkpoint.
- [ ] **Step 6: Run schema and full control-plane tests.** Verify deterministic errors and stable reason codes.

## Task 8: Run Summary and G10 Optimization

**Files:**
- Create: `tom-autodev/scripts/run_summary.py`
- Modify: `orchestrator.py`, `state_store.py`, `artifact_store.py`, `tom-autodev/SKILL.md`
- Test: `tom-autodev/scripts/tests/test_run_summary.py`, `test_fake_e2e.py`

**Interfaces:**
- `RunSummary.build(run_id: str) -> RunSummaryArtifact`
- `RunSummary.propose(summary: RunSummaryArtifact, allowed_roots: list[Path]) -> OptimizationProposal`
- `RunSummary.apply(proposal_id: str, approval_id: str) -> dict`

- [ ] **Step 1: Write failing tests for summary and guardrails.** Cover successful/failed/timeout runs, failure grouping, secret redaction, forbidden business/iPipe/profile mutations, G10 hash binding, reject, apply, validation failure and rollback.
- [ ] **Step 2: Run summary tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_run_summary.py -v`.
- [ ] **Step 3: Implement summary aggregation.** Read events, approvals, artifacts, collaboration receipts and pipeline evidence to produce stable metrics and signatures.
- [ ] **Step 4: Implement proposal generation.** Emit evidence, root cause, target files, candidate diff, expected benefit, risk, rollback and exact verification commands; redact tokens and personal data where not required.
- [ ] **Step 5: Implement G10 application.** Require approval bound to candidate hash, apply only allowed roots, run validation, rollback on failure, and archive proposal/result in KU.
- [ ] **Step 6: Run full control-plane tests.** Confirm no production repository or pipeline template is touched.

## Task 9: Update Stage, Language and Project Skills

**Files:**
- Modify: `tom-autodev/SKILL.md`, `tom-grill/SKILL.md`, `tom-spec/SKILL.md`, `tom-tasks/SKILL.md`, `tom-plan/SKILL.md`, `tom-implement/SKILL.md`, `tom-review/SKILL.md`, `tom-diagnose/SKILL.md`, `tom-lang-c-cpp/SKILL.md`, `tom-lang-npl/SKILL.md`, `tom-project-bgw/SKILL.md`, `tom-project-xflow/SKILL.md`, `setup-tom-autodev/SKILL.md`
- Create/modify: referenced phase templates and checklists under each Skill’s `references/`
- Test: `scripts/quick_validate.py` for every modified Skill

- [ ] **Step 1: Add the Comate-only boundary and embedded-runtime rule.** Remove Codex and standalone `tom-autorelease` references from trigger/workflow text.
- [ ] **Step 2: Add phase input/output protocol references.** Each phase Skill must list required ArtifactEnvelope inputs, specialized output schema, KU persistence, iCafe comment, approval gate and stop conditions.
- [ ] **Step 3: Add C/C++ and NPL concrete review/test checklists.** Include source-review categories, external test interface, fixtures, failure classification and iPipe-only execution rules.
- [ ] **Step 4: Add project-specific test/environment/knowledge graph requirements.** Ensure BGW and XFlow profiles cannot infer test repositories or tool limits.
- [ ] **Step 5: Run quick validation.** Run `python3 /Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py` for each changed Skill directory and fix all failures.

## Task 10: Fake End-to-End and Read-Only Live Preflight

**Files:**
- Create: `tom-autodev/scripts/tests/test_fake_e2e.py`, `tom-autodev/scripts/tests/test_live_preflight.py`
- Modify: `tom-autodev/scripts/cli.py`, `orchestrator.py`
- Test: all `tom-autodev/scripts/tests/`

- [ ] **Step 1: Write failing fake-E2E scenarios.** Define BGW and XFlow fixtures with iCafe/KU/Infoflow/iCode/iPipe fake transports and assert the full state/event/artifact/group/role-routing trace.
- [ ] **Step 2: Run fake-E2E tests and verify RED.** Run `python3 -m unittest tom-autodev/scripts/tests/test_fake_e2e.py -v`.
- [ ] **Step 3: Implement orchestration wiring.** Connect profile, protocol, knowledge sync, collaboration, review action, iCode runtime, iPipe runtime and summary into one controller; expose `next`, `complete-phase` and `optimize` CLI commands.
- [ ] **Step 4: Implement failure and recovery scenarios.** Cover code failure -> @研发, test/environment failure -> @测试, mixed failure -> both, crash after external intent, duplicate callback and G10 reject/apply.
- [ ] **Step 5: Run the complete control-plane suite.** Run `python3 -m unittest discover -s tom-autodev/scripts/tests -v` and `python3 -m compileall -q tom-autodev/scripts`.
- [ ] **Step 6: Run read-only live preflight.** Check iCafe login/version, KU target access, Comate iCode preflight, Review component presence, Infoflow configuration and iPipe discovery. Do not create groups, write KU/iCafe, submit iCode, trigger iPipe, or execute project commands.

## Task 11: Non-Production iPipe Dry-Run Acceptance

**Files:**
- Modify: project profiles under the configured `~/.tom-autodev/config/projects/` only after setup confirmation.
- Create: `tom-autodev/docs/superpowers/acceptance/2026-08-10-bgw-xflow-dry-run.md`

- [ ] **Step 1: Confirm profiles.** Human confirms unique BGW/XFlow business repositories, independent test repositories, module/branch, pipeline ID, environment fingerprint, release rule, team emails and KU parent.
- [ ] **Step 2: Confirm G0 collaboration rosters.** Human reviews group name, owner, dev/test members and role mappings.
- [ ] **Step 3: Run BGW non-production dry-run.** Execute only through approved iPipe: code/test generation, Review, iCode, compile/unit/regression/integration, failure routing if injected, and release evidence verification.
- [ ] **Step 4: Run XFlow NPL non-production dry-run.** Use NCS/compiler/simulator only in the configured iPipe runner; verify chip/resource diagnostics and test responsibility routing.
- [ ] **Step 5: Record evidence.** Store run summary, collaboration receipts, iPipe evidence and known gaps in the acceptance document and project KU parents.
- [ ] **Step 6: Do not enable production automatically.** Production enablement remains a separate human decision after both dry-runs satisfy all acceptance criteria.

## Dependency Order

```text
Task 1 -> Task 2 -> Task 3
                 |
                 +-> Task 4 -> Task 5 -> Task 6
                 |
                 +-> Task 7 -> Task 8
Task 4/5/6/7/8 -> Task 9 -> Task 10 -> Task 11
```

Task 1 is the first safety checkpoint. Tasks 4-6 may be implemented in separate commits after state/profile foundations are stable; Task 7 consumes their receipts. Task 11 is blocked until real profile data and human roster confirmation exist.

## Verification Commands

Control-plane only:

```bash
python3 -m unittest discover -s tom-autodev/scripts/tests -v
python3 -m compileall -q tom-autodev/scripts
python3 /Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py tom-autodev
```

Run the corresponding `quick_validate.py` command for every changed child Skill. Never replace these checks with BGW/XFlow build or test commands on Mac.

