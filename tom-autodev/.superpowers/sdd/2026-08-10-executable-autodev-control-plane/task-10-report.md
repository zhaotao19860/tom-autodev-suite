# Task 10 Implementation Report

Date: 2026-08-11

## Status

Local implementation is complete, but Task 10 remains open on external blocker I1. This report covers fake-only tests and the separately authorized read-only live-preflight boundary. No live service write or local BGW/XFlow build, unit, regression, integration, Docker, NCS, simulator, or release command was run.

## Production wiring

- `Orchestrator` remains the single Comate-only control surface.
- Added `next(run_id)` over the existing `PhaseProtocol.next()` boundary.
- Added `complete_phase(run_id, envelope)` over the existing pinned `KnowledgeSync` and `PhaseProtocol.complete()` boundary.
- Added `optimize(run_id, operation)` for RunSummary `build`, G10 `propose`, and G10 `apply`; propose/apply resolve the run-pinned KnowledgeSync and preserve the existing archived approval/receipt rules.
- Added `trace(run_id)` for the durable event, artifact, collaboration receipt, project/KU binding, and canonical failure-role trace.
- Preserved the existing controller-owned collaboration, Review child action, iCode runtime, iPipe runtime, SUBMIT->IPIPE and release boundaries. No callable write backend was added.
- Added CLI commands `next`, `complete-phase`, `optimize`, and `preflight` to the existing `start/status/approve/resume/stop` entrypoint. `scripts/cli.py` remains only a forwarding entrypoint to `orchestrator.main()`.

## Fake E2E trace assertions

BGW fixture:

- project `bgw`, iCafe `BGW-*`, KU repo `sX0BTOBWJX`, parent `I15ClP2KW4ZGAK`;
- first action is `host=comate`, `phase=INTAKE`;
- trace binds the exact project/profile and starts with only the durable `INTAKE` event.

XFlow fixture:

- project `xflow`, iCafe `XFLOW-*`, KU repo `sX0BTOBWJX`, parent `meQ-Acjg0K09Xr`;
- first action is `host=comate`, `phase=INTAKE`;
- trace binds the exact project/profile and does not infer a BGW target.

Additional fake-only assertions:

- durable artifact and Infoflow group receipt appear in one trace;
- code failure category `code` resolves to role `development`;
- `test-case` and `environment` resolve to role `test`; `mixed` resolves to both `development` and `test`;
- duplicate failure callback returns the same event/result;
- a crash after an external KU intent returns `RECOVERY_REQUIRED` and `retry_allowed=false`;
- G10 `build/propose/apply` dispatch is through one RunSummary boundary;
- existing RunSummary suite continues to cover durable G10 rejection, approval-bound apply, rollback, recovery, archive and replay behavior;
- existing collaboration/iCode/iPipe suites continue to exercise their real runtime classes over fake transports and temporary SQLite/Git artifacts.

## Read-only preflight

The preflight requires exactly six dependency-injected query probes: iCafe, KU, Comate iCode, Review component, Infoflow and iPipe. Tests prove the controller calls only `query()`, does not create events or external intents, withholds role-member data from probe inputs, redacts secrets/personal data, rejects untrusted reason text, and returns `PROJECT_NOT_READY` before any query when a profile or probe is missing. Production probe objects expose none of `create`, `update`, `comment`, `submit`, `trigger`, `release`, or `rerun`.

Allowed production queries are limited to:

- iCafe version and login status;
- KU target `query-repo` access;
- iCode version/login status against configured repositories;
- Review executable presence (the Review command is not executed);
- Infoflow `setup.sh --check` configuration check;
- iPipe GET discovery of the exact configured pipeline/module.

Actual read-only live preflight result: `NOT_RUN`.

Environment blocker: `/Users/tom/.tom-autodev/config/projects` does not exist and contains no confirmed BGW/XFlow profiles. Running a live probe without confirmed repository, pipeline, environment and tool identities would violate the required `PROJECT_NOT_READY` behavior. The root controller must run `tom-autodev preflight <project>` only after those profiles are confirmed. No live iCafe/KU/Infoflow/iCode/iPipe request was made by this implementation task.

## RED evidence

1. After correcting test-module/fixture errors, fake E2E failed because `Orchestrator.next`, `optimize`, `trace`, and `preflight` did not exist.
2. CLI RED failed with argparse `invalid choice` for `next`, `complete-phase`, `optimize`, and `preflight`.
3. A dedicated security RED proved that arbitrary probe `reason_code` text could bypass output redaction. The final code accepts only stable uppercase reason codes and maps all other text to `PREFLIGHT_QUERY_FAILED`.
4. A live KU argv RED proved the probe used unsupported `--offset/--limit` flags; the implementation now uses the documented read-only `--page-num 1 --page-size 1` contract.
5. An initial test expectation incorrectly conflated failure categories (`test-case/environment/mixed`) with resolved roles (`test/test/both`). It was corrected before production behavior was changed; existing taxonomy compatibility was preserved.

## GREEN verification

- Focused fake E2E + preflight: 13/13 PASS.
- Exact brief fake E2E command from `/Users/tom/Desktop/skills`: 6/6 PASS.
- Full control-plane discovery: 418/418 PASS in 26.273 seconds.
- Named schema validation: 14/14 PASS.
- `python3 -m compileall -q scripts`: PASS.
- CLI help exposes exactly `start,status,approve,resume,stop,next,complete-phase,optimize,preflight`: PASS.
- Targeted safety scan: no `tom-autorelease`, Codex/Claude, `shell=True`, or `os.system` references in changed production/test files; no local build/test/release command vector in changed production files. The sole subprocess call added is the bounded iCode version/login query adapter.
- No Git repository exists at `/Users/tom/Desktop/skills`; no Git initialization or fabricated commit was performed there.

The Homebrew Python 3.14 exact-path run emitted `ResourceWarning` messages for SQLite connections created by the pre-existing store implementation; the project-directory Python full run was clean. All tests passed. This is recorded as residual interpreter-specific resource hygiene rather than changed in Task 10.

## SHA-256

- `scripts/orchestrator.py`: `9d6528770f0d7feb742511daa5a48f1cd3dc0e0cc6ea543758b4e6d66eb74ca5`
- `scripts/cli.py`: `294bba0b3d55e9eccd7fc64c3183d08f586dfd6177c466c26056eeaf35988305`
- `scripts/preflight.py`: `335f05b6c2b73bdfbfa9461ea7508c88c49905f5a8aeb142f66ee4725d46ff94`
- `scripts/tests/test_fake_e2e.py`: `1be20b7fd0588d4dcf9d1b9c70a547a6b68d49c33771f3e2f41b20fa2d2f5647`
- `scripts/tests/test_live_preflight.py`: `45391cb48cd0f49dde8223845cbc6cd4442103c3f44f1e9ace65748122ee484c`

Schema hashes were collected and all schema files were behaviorally validated by the 14-test named-schema suite; Task 10 did not modify schema files.

## Concerns

- Actual read-only live preflight remains pending confirmed BGW/XFlow profiles and explicit execution by the root controller.
- The Python 3.14 exact-path focused run surfaces pre-existing unclosed-SQLite `ResourceWarning` diagnostics; no functional failure occurred.
- No real pipeline acceptance is claimed. Non-production iPipe dry-run remains Task 11 and requires confirmed profiles/rosters plus explicit authorization.

## Fix Round 1 - 2026-08-11

### Review findings

- I2 is fixed. One parameterized integrated fake E2E drives both BGW and XFlow through the actual `CafeClient.snapshot()`, canonical `CollaborationSession` G0 group receipt, `PhaseProtocol`, `KnowledgeSync`, Review child action, `submit_to_ipipe()` with a durable fake iCode receipt, actual `IpipeRuntime`, failure routing, actual controller-owned `RunSummary`, and G10 reject/apply boundaries.
- I3 is fixed. `Orchestrator.optimize()` no longer accepts a replacement summary runtime, rejects missing runs, validates the pinned profile, and always constructs its owned `RunSummary`. The only test seam is a private constructor-option dictionary read through the explicit `control_root` and `validation_runner` keys.
- I4 is fixed. Preflight accepts only canonical project IDs and rejects `project != profile.project_id` before constructing or calling probes.
- I5 is fixed. Preflight delegates nested diagnostic redaction to the Task 8 canonical structured redactor, covering Authorization/Bearer values, API keys, token assignments, URL credentials/query secrets, email, and phone data.
- I6 is fixed. Trace resolves KU identity only through the hash-validated run-pinned profile and returns `PROFILE_CONFLICT` after profile drift.
- I7 is fixed. The iCode preflight query closes stdin, supplies no token, bounds output, calls login once, and accepts only `Already logged in as:` status output. Exact query contracts are covered for all six probes.
- I1 remains open as an external environment blocker. `/Users/tom/.tom-autodev/config/projects` is absent. BGW and XFlow preflight each returned `PROJECT_NOT_READY`, `components={}`, and `missing=["profile_file"]` before any probe. No six-component live result is claimed or fabricated.

### Integrated fake trace

Both project cases traverse this exact event sequence without direct `StateStore.transition()` seeding inside the integrated helper:

`INTAKE -> GRILL -> SPEC -> TASKS -> WORKSPACE -> PLAN -> IMPLEMENT -> REVIEW -> SUBMIT -> IPIPE -> RELEASE -> DIAGNOSE`

The BGW case pins KU repo `sX0BTOBWJX`, parent `I15ClP2KW4ZGAK`; the XFlow case pins KU repo `sX0BTOBWJX`, parent `meQ-Acjg0K09Xr`. Each case asserts the exact project/profile trace, phase/submission/iPipe/run-summary artifact kinds, group-create and failure-message receipts, and code-failure routing to `development`.

The iPipe fake first raises an unknown trigger result after installing the exact remote build. The actual runtime reconciles that build, persists the receipt, and replays the same result on the duplicate call while the fake API records one trigger write. Monitoring and iPipe evidence ingestion then use the actual runtime/protocol boundaries. The BGW G10 proposal is rejected and leaves the target bytes unchanged; the XFlow G10 proposal is approved, validated by the narrow fake validation runner, archived through actual `KnowledgeSync`, and changes only the temporary control-plane target.

### Additional production corrections exposed by RED

- `advance()` now promotes only validated `task_id` and complete business/test `source_revisions` into the committed event, allowing the next PLAN action to consume the approved workspace baseline.
- `KnowledgeSync` now returns and durably records `schema_version: "1"`, matching the existing RunSummary archive-receipt contract.
- Trace and RunSummary count both legacy `collaboration.*` receipts and the actual `infoflow.group.*` operations emitted by `CollaborationSession`.

Stable RED failures were `SOURCE_REVISION_REQUIRED`, `G10_ALLOWED_ROOT_INVALID`, `G10_ARCHIVE_FAILED`, an empty collaboration trace, and a zero RunSummary collaboration count. Fixture-only REDs for missing `evidence_revisions` and a deliberately forbidden project-named G10 directory were corrected without weakening production policy.

### Verification

- Focused Task 10 suite: 20/20 PASS.
- Full control-plane suite: 425/425 PASS in 22.280 seconds after the final constructor-seam narrowing.
- Named schema suite: 14/14 PASS.
- `python3 -m compileall -q scripts`: PASS.
- `python3 scripts/cli.py --help`: PASS; exposes `start,status,approve,resume,stop,next,complete-phase,optimize,preflight`.
- Safety scan: no Codex, Claude, `tom-autorelease`, `shell=True`, or `os.system` reference in the changed production/test surface. Production subprocess sites remain the G10 allowlisted Python unittest validator and the iCode query adapter with closed stdin; no BGW/XFlow build, unit, regression, integration, Docker, NCS, simulator, or release command was run.
- No live service write or live service probe was made. Tests used temporary Git repositories, SQLite/artifact roots, fake transports, and fake APIs only.

### Fix-round hashes

- `scripts/orchestrator.py`: `822dda6ce59165f7eb0815eec23f4e39ac1ba800b07febe18eb4e61f094c918f`
- `scripts/preflight.py`: `efd9b713abb1b8111e8bd9dc01c4f7d7767b864ba96362713a8c8adaaf2a0c0d`
- `scripts/knowledge_sync.py`: `be48d33888701181993d2f865789356532553ee22b63d5f15d65ae110cf5a03f`
- `scripts/run_summary.py`: `82cc200dfb3cd15408f8585f2030f3e08a50da00bd4ff95528cd25f680035d22`
- `scripts/tests/test_fake_e2e.py`: `692b54c427105bd9de655c83430e9077fcf0996e399f5e12d0d39d021bd5c7c1`
- `scripts/tests/test_live_preflight.py`: `771072fa2a4aa05fb0c3698f4473b3dff3674a586999414ee3c14ed9050ff05c`
- `scripts/cli.py` (unchanged): `294bba0b3d55e9eccd7fc64c3183d08f586dfd6177c466c26056eeaf35988305`

## Fix Round 2 - 2026-08-11

### Review findings

- I2 is addressed by replacing both direct-state placeholders. Test-case, environment, and mixed failures now traverse the production `CafeClient`, G0 `CollaborationSession`, phase protocol, owned workspace boundary, submit controller, actual `IpipeRuntime`, iPipe evidence ingestion, `route_failure()`, and actual collaboration message routing. The asserted recipients are respectively test, test, and development plus test.
- I2 crash recovery now creates a real `ipipe.trigger` intent inside production runtime code, installs the exact remote build, and raises `SystemExit` before any receipt to model process death. A fresh `Orchestrator` over the same durable stores returns `RECOVERY_REQUIRED`; a fresh iPipe runtime discovers the exact remote build, closes the original intent, and replays the same receipt without a second trigger write.
- I3 is addressed. G10 rejects summaries, proposals, and approvals owned by another run before constructing KnowledgeSync. `optimize()` exposes no KnowledgeSync injection. Tests no longer patch the whole KnowledgeSync factory; the only private seam accepts `ku_transport`, `cafe_transport`, `username`, and `cafe_preflight`, while production always constructs and validates the run/card/fixed-parent-pinned `KnowledgeSync` instance.
- I5 is addressed with a fixed preflight reason-code allowlist. Unknown success values normalize to `OK`; unknown failures, including uppercase secret-shaped values such as `TOKEN_TOP_SECRET` and `AUTHORIZATION_BEARER_SECRET`, normalize to `PREFLIGHT_QUERY_FAILED` and never reach output.
- N1 is addressed by the controller-owned `workspace_binding()` boundary. It derives the task from the durable TASKS DAG frontier through `PhaseProtocol.next(WORKSPACE)`, verifies business and test ownership through `WorkspaceManager.query_ownership()`, enforces exact profile module/path role binding, ACTIVE registration, run/task/worktree/owner/baseline identity, and hashes the complete canonical binding into G4.
- N1 persists only the derived task, revisions, owner-token hash proof, profile hash, source WORKSPACE event, and canonical repository/worktree identities. Raw owner tokens and caller receipts are not persisted. Caller task/revision aliases are equality assertions only; mismatches fail before transition. An unbound compatibility checkpoint persists no task, revisions, or workspace binding and remains fail-closed with `SOURCE_REVISION_REQUIRED`.
- I1 remains OPEN. `/Users/tom/.tom-autodev/config/projects` is absent, so no six-component read-only live preflight is claimed or fabricated.

### RED and regression evidence

The stable N1 RED consisted of three failures: a forged task/revision transition returned `OK` instead of `WORKSPACE_BINDING_REQUIRED`, while both positive ownership and caller-mismatch tests failed because `Orchestrator.workspace_binding()` did not exist. After the production repair all three passed, followed by explicit role-swap and unbound-checkpoint regressions.

The new I2 production-boundary tests did not expose a production runtime defect: restart reconciliation already existed. They instead replaced the rejected direct `StateStore.transition()` and arbitrary-intent placeholders. Initial failures were fixture-only (the configured test member is `tester@example.test`, and repeated scenarios reused one temporary G10 path); both were corrected without changing production routing or recovery behavior.

The root-directory planned command initially failed because `test_fake_e2e.py` imported a fake from another test module whose package path was cwd-sensitive. The fake runtime is now local to the E2E test, removing that cross-test dependency; the exact command passes without `PYTHONPATH` changes.

### Verification

- Exact focused command from `/Users/tom/Desktop/skills`: 28/28 PASS.
- Full control-plane discovery: 433/433 PASS, zero failures and zero errors.
- Named schema validation: 14/14 PASS.
- `python3 -m compileall -q scripts`: PASS.
- `python3 scripts/cli.py --help`: PASS with `start,status,approve,resume,stop,next,complete-phase,optimize,preflight`.
- Safety scan: no Codex, Claude, `tom-autorelease`, `shell=True`, or `os.system` reference in the changed production/test surface. No local BGW/XFlow build, unit, regression, integration, Docker, NCS, simulator, or release command was run.
- No live service request or write was made. Tests used fake transports/APIs, temporary SQLite/artifacts, and temporary Git repositories/worktrees only.
- The root-directory focused command emits the previously recorded unclosed-SQLite `ResourceWarning` diagnostics under the app-selected Python interpreter; all 28 tests pass and no functional failure is hidden.

### Fix-round hashes

- `scripts/orchestrator.py`: `77c3b3d97f5c8e937891918d661502abecf721d268fa3a30d77ba2198d2cb795`
- `scripts/preflight.py`: `702b0a4cd6d169fa2b94a0e07ba5e56b64c6559983ed6dce5e65d6ea896732d9`
- `scripts/knowledge_sync.py` (unchanged in round 2): `be48d33888701181993d2f865789356532553ee22b63d5f15d65ae110cf5a03f`
- `scripts/run_summary.py` (unchanged in round 2): `82cc200dfb3cd15408f8585f2030f3e08a50da00bd4ff95528cd25f680035d22`
- `scripts/tests/test_fake_e2e.py`: `d7328f816b5ffaa6c3228a4729462bdfe081a108f6a1bd3ecd654064e4ee843d`
- `scripts/tests/test_live_preflight.py`: `78f218b3e21ccdb81b330f08e653505b3a08883c211f3304be4abd2f20111685`
- `scripts/cli.py` (unchanged): `294bba0b3d55e9eccd7fc64c3183d08f586dfd6177c466c26056eeaf35988305`

## Fix Round 3 - 2026-08-11

### N1 repair

- WORKSPACE -> PLAN now constructs a new canonical G4 evidence object field by field. It never persists the caller evidence mapping. The durable object contains only the run-bound input and ledger approval, the exact controller-required `workspace` and `task-plan` artifact names, ownership-derived task/revisions, and the canonical workspace binding.
- Extra caller artifact names and `required_artifacts` cannot become durable evidence. Unknown fields, nested aliases, opaque proofs, duplicate receipts under another key, and non-contract timestamp/environment fields are discarded before the evidence gate and persistence boundaries.
- A truthy caller `blocking_findings` value fails immediately with `BLOCKING_FINDING` and is not persisted; arbitrary finding text is never copied into a successful event.
- The raw owner token remains usable only for `WorkspaceManager.query_ownership()`. The durable binding contains its SHA-256 proof, while the raw receipt and every caller copy are removed before transition/idempotency persistence.
- If the caller supplies `workspace_binding` together with real receipts, it must be deeply equal to the complete derived binding. Forged objects and near matches that alter either worktree identity or owner proof return `WORKSPACE_BINDING_MISMATCH` before transition. An exact supplied binding passes.
- The unbound compatibility path is preserved: it records only the safe G4 policy evidence, no task/revision/binding, and the next PLAN action remains blocked with `SOURCE_REVISION_REQUIRED`.
- I2, I3, and I5 remain addressed. I1 remains OPEN because confirmed live project profiles are still absent; no live preflight was run.

### RED evidence

Before the production edit, the four new adversarial test methods produced four failures and one subtest error:

- the real owner token was durably present in `artifacts`, `required_artifacts`, `opaque_proof`, and nested aliases after a successful transition;
- a full receipt copy under `workspace_receipts_copy` reached persistence and raised `PERSISTENCE_SECRET_REJECTED` instead of being projected away;
- a forged `{"forged": true}` binding returned `OK` instead of `WORKSPACE_BINDING_MISMATCH`;
- near-match worktree-path and owner-proof mutations both returned `OK`.

After canonical projection and deep alias comparison, all four methods pass. The existing five N1 ownership, alias, role-swap, and unbound compatibility tests also pass unchanged.

### Verification

- Exact focused command from `/Users/tom/Desktop/skills`: 32/32 PASS.
- Full control-plane discovery: 437/437 PASS, zero failures and zero errors.
- Named schema validation: 14/14 PASS.
- `python3 -m compileall -q scripts`: PASS.
- `python3 scripts/cli.py --help`: PASS with `start,status,approve,resume,stop,next,complete-phase,optimize,preflight`.
- Safety scan: no Codex, Claude, `tom-autorelease`, `shell=True`, or `os.system` reference in the changed production/test surface. The unchanged preflight subprocess site remains the bounded read-only iCode query adapter.
- No live request/write or local BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator, or release command was run. Tests used fake transports/APIs and temporary SQLite, artifacts, Git repositories, and worktrees only.
- The root-directory focused command emitted the already-recorded unclosed-SQLite `ResourceWarning` diagnostics under Homebrew Python 3.14; all 32 tests passed.

### Fix-round hashes

- `scripts/orchestrator.py`: `497030ba0ca86c75a2bb6d3b5392feb4c59ac211229ca7816a9b06542a97863a`
- `scripts/tests/test_fake_e2e.py`: `76b4beab2a544fa7b82741a1b7f3901ec5ff54866109b3a2bc7d092d81f81630`
- `scripts/preflight.py` (unchanged): `702b0a4cd6d169fa2b94a0e07ba5e56b64c6559983ed6dce5e65d6ea896732d9`
- `scripts/knowledge_sync.py` (unchanged): `be48d33888701181993d2f865789356532553ee22b63d5f15d65ae110cf5a03f`
- `scripts/run_summary.py` (unchanged): `82cc200dfb3cd15408f8585f2030f3e08a50da00bd4ff95528cd25f680035d22`
- `scripts/tests/test_live_preflight.py` (unchanged): `78f218b3e21ccdb81b330f08e653505b3a08883c211f3304be4abd2f20111685`
- `scripts/cli.py` (unchanged): `294bba0b3d55e9eccd7fc64c3183d08f586dfd6177c466c26056eeaf35988305`
