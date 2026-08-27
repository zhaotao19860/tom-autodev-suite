# Task 10: Fake End-to-End and Read-Only Live Preflight

## Files

- Create: `tom-autodev/scripts/tests/test_fake_e2e.py`
- Create: `tom-autodev/scripts/tests/test_live_preflight.py`
- Modify/create: `tom-autodev/scripts/cli.py`
- Modify: `tom-autodev/scripts/orchestrator.py`
- Test: all `tom-autodev/scripts/tests/`

## Required steps

1. Write failing fake-E2E scenarios. Define BGW and XFlow fixtures with iCafe/KU/Infoflow/iCode/iPipe fake transports and assert the full state/event/artifact/group/role-routing trace.
2. Run `python3 -m unittest tom-autodev/scripts/tests/test_fake_e2e.py -v`; expect RED before wiring exists.
3. Implement orchestration wiring. Connect profile, protocol, KnowledgeSync, collaboration, review action, iCode runtime, iPipe runtime and RunSummary into one controller; expose `next`, `complete-phase` and `optimize` CLI commands in addition to the strict existing start/status/approve/resume/stop surface.
4. Implement failure and recovery scenarios. Cover code failure -> @研发, test/environment failure -> @测试, mixed failure -> both, crash after external intent, duplicate callback and G10 reject/apply.
5. Run `python3 -m unittest discover -s scripts/tests -v` and `python3 -m compileall -q scripts` from `/Users/tom/Desktop/skills/tom-autodev`.
6. Implement and run read-only live preflight. Check iCafe login/version, KU target access, Comate iCode preflight, Review component presence, Infoflow configuration and iPipe discovery. Do not create groups, write KU/iCafe, submit iCode, trigger iPipe, execute project commands, or release.

## Binding global constraints

- Comate is the only supported host; no Codex compatibility or second entrypoint.
- `tom-autorelease` is source material only; production code must not import or execute it.
- Mac may inspect source, generate source/tests, manage worktrees, run source Review, and parse remote evidence only.
- Never run BGW/XFlow compilation, unit, regression, integration, Docker, NCS, simulator, local substitutes, or release commands.
- All state transitions and external side effects fail closed and are idempotent.
- Tests use fake transports, temporary SQLite/artifact roots and temporary Git repositories/worktrees only.
- Never perform live iCafe/KU/Infoflow/iCode/iPipe writes during tests or preflight.
- BGW KU target: repo `sX0BTOBWJX`, parent `I15ClP2KW4ZGAK`.
- XFlow KU target: repo `sX0BTOBWJX`, parent `meQ-Acjg0K09Xr`.
- Do not initialize Git in `/Users/tom/Desktop/skills` or fabricate commits.

## Contracts to preserve

- Strict CLI start captures a canonical iCafe snapshot before run creation and returns nonzero for non-ready results.
- G0 collaboration requires the canonical group/member/session receipt; tests must not make live group writes.
- `submit_to_ipipe()` is the only production SUBMIT->IPIPE ownership path and binds exact durable iCode intent/receipt, profile, revisions, environment and G7.
- `PhaseProtocol` owns child phase action/result envelopes and KnowledgeSync publication; controller-owned WORKSPACE/SUBMIT/IPIPE/RELEASE remain descriptor/action boundaries.
- `RunSummary`/G10 requires archived summary/proposal/result receipts, exact approval hash, control-plane-only roots, durable recovery, and no profile/pipeline/business mutation.
- Role routing uses canonical failure classes: code -> development, test/environment -> test, mixed -> both.

## Read-only live preflight behavior

- Provide a callable/CLI preflight that is independently testable with fake adapters.
- Default/live adapters may invoke only documented query/version/login/status/discovery/config-check commands.
- Return a structured per-component result and overall `READY`/`PREFLIGHT_FAILED`; redact secrets and personal data.
- Missing profiles or unconfirmed project values must return `PROJECT_NOT_READY`, not infer repository/pipeline/test/tool identities.
- A preflight failure must not create state transitions, intents for remote writes, groups, documents, comments, CRs, builds, reruns, or release actions.

## Report contract

Append full Task 10 implementation report to `/Users/tom/Desktop/skills/tom-autodev/.superpowers/sdd/2026-08-10-executable-autodev-control-plane/task-10-report.md`. Include fake-E2E trace assertions for BGW/XFlow, RED/GREEN evidence, focused/full test results, compile/schema/safety scans, read-only live preflight exact results and any environment blockers, hashes and concerns. Return only status, verification summary and concerns.
