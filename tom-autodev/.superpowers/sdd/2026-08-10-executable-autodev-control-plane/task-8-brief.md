# Task 8: Run Summary and G10 Optimization

## Files

- Create: `tom-autodev/scripts/run_summary.py`
- Modify: `tom-autodev/scripts/orchestrator.py`, `tom-autodev/scripts/state_store.py`, `tom-autodev/scripts/artifact_store.py`, `tom-autodev/SKILL.md`
- Test: `tom-autodev/scripts/tests/test_run_summary.py`, `tom-autodev/scripts/tests/test_fake_e2e.py`

## Interfaces

- `RunSummary.build(run_id: str) -> RunSummaryArtifact`
- `RunSummary.propose(summary: RunSummaryArtifact, allowed_roots: list[Path]) -> OptimizationProposal`
- `RunSummary.apply(proposal_id: str, approval_id: str) -> dict`

## Required steps

1. Write failing tests for summary and guardrails. Cover successful/failed/timeout runs, failure grouping, secret redaction, forbidden business/iPipe/profile mutations, G10 hash binding, reject, apply, validation failure and rollback.
2. Run `python3 -m unittest tom-autodev/scripts/tests/test_run_summary.py -v`; expect RED because the implementation is absent/incomplete.
3. Implement summary aggregation. Read events, approvals, artifacts, collaboration receipts and pipeline evidence to produce stable metrics and signatures.
4. Implement proposal generation. Emit evidence, root cause, target files, candidate diff, expected benefit, risk, rollback and exact verification commands; redact tokens and personal data where not required.
5. Implement G10 application. Require approval bound to candidate hash, apply only allowed roots, run validation, rollback on failure, and archive proposal/result in KU.
6. Run the full control-plane suite. Confirm no production repository or pipeline template is touched.

## Binding global constraints

- Comate is the only supported host; do not add Codex compatibility or a second entrypoint.
- `tom-autorelease` is source material only; production code must not import or execute it.
- Mac may inspect source, generate source/tests, manage worktrees, run source Review, and parse remote evidence only.
- Never run BGW/XFlow compilation, unit, regression, integration, Docker, NCS, simulator, or release commands.
- All state transitions and external side effects are fail-closed and idempotent.
- Every phase artifact is versioned, content-hashed, schema-validated, published to KU, and linked back to iCafe.
- G10 is required before applying any Skill/control-plane optimization.
- Tests are control-plane, adapter-contract, and fake-E2E tests only.
- Do not initialize Git or fabricate commits in `/Users/tom/Desktop/skills`.
- Do not make live iCafe, KU, Infoflow, iCode, or iPipe writes. Use fake transports and temporary SQLite/artifact/profile roots.

## Earlier interfaces to preserve

- `PhaseProtocol` already owns phase artifact validation/publication and G10 is terminal post-run scope; do not weaken it.
- `Orchestrator` owns strict Comate/iCafe snapshot start, durable state, and the production `submit_to_ipipe()` controller path.
- `StateStore` and `ArtifactStore` are SQLite/file-backed and must remain atomic, content-hashed, and replay-safe.
- The existing `KnowledgeSync` client is the only publication boundary; proposal application must not call raw KU/iCafe clients directly.

## Report contract

Append a complete Task 8 implementation report to `/Users/tom/Desktop/skills/tom-autodev/.superpowers/sdd/2026-08-10-executable-autodev-control-plane/task-8-report.md`. Include status, design decisions, changed files, TDD RED/GREEN commands and outputs, focused/full verification, compile/schema/safety scans, file hashes, and concerns. Return only status, one-line test summary, and concerns in the agent response.
