# Task 10 Fix Round 3 Scoped Review Package

No Git repository exists. Review the current files and exact round-3 hashes in `task-10-report.md`; do not infer a commit range.

## Inputs

- Brief: `task-10-brief.md`
- Prior scoped review: `task-10-rereview-round2.md`
- Cumulative report: `task-10-report.md`, section `Fix Round 3 - 2026-08-11`
- Finding to verdict: `N1`
- Findings to preserve: `I2`, `I3`, `I5` remain ADDRESSED
- External blocker to preserve: `I1` remains OPEN

## Round-3 files

- `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py`
- `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_fake_e2e.py`

## Review scope

Verdict N1 ADDRESSED or NOT ADDRESSED. Verify that WORKSPACE -> PLAN constructs its durable G4 evidence field by field from the fixed PLAN artifact contract, the ledger-fetched approval, and WorkspaceManager-derived task/revision/binding values. Confirm that raw owner tokens cannot persist through ordinary values, nested aliases, extra artifact names, custom required artifacts, or duplicated receipts under another key. Confirm that a caller-supplied workspace binding must exactly equal the derived complete binding, with forged and near-match values rejected before transition. Confirm that the unbound compatibility checkpoint persists no executable task/revision/binding and that `PhaseProtocol.next()` returns `SOURCE_REVISION_REQUIRED`.

Flag new Critical/Important breakage only in the round-3 files. Preserve the round-2 I2/I3/I5 verdicts and do not treat missing live profiles as repaired.

## Global constraints

- Comate only; no Codex compatibility.
- No `tom-autorelease` runtime dependency.
- No live calls or writes.
- No local BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator, or release command.
- Tests may use only fake transports/APIs and temporary SQLite, artifacts, and Git repositories/worktrees.
