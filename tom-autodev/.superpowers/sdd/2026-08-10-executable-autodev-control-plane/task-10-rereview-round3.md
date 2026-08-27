# Task 10 Fix Round 3 Scoped Re-review

Date: 2026-08-11

Reviewed the round-3 package, the two changed files, and the preserved finding
evidence. Both round-3 hashes match the files on disk. This review used static
inspection, the exact focused fake-only suite, and temporary-state adversarial
reproduction only. It made no live request, ran no BGW/XFlow project command,
and performed no Git operation.

## Finding Verdicts

### I1 - OPEN (external blocker)

Confirmed BGW/XFlow live project profiles remain absent. No six-component live
preflight is claimed or inferred. This remains an external Task 10 readiness
blocker until profiles are confirmed and a separately authorized query-only
preflight is completed.

### I2, I3, I5 - ADDRESSED (preserved)

The round-3 change is restricted to WORKSPACE -> PLAN evidence construction and
its fake-E2E regressions. Static inspection found no change that reopens the
round-2 production iPipe recovery/routing, G10 run ownership, or preflight
reason-code allowlist repairs. Their ADDRESSED verdicts are preserved.

### N1 - ADDRESSED

The ownership boundary remains controller-derived. `workspace_binding()` obtains
the current task from `PhaseProtocol.next(WORKSPACE)`, requires exact business
and test receipt roles, matches module/path to the pinned profile, verifies the
WorkspaceManager run/task/token/ACTIVE registration/worktree/baseline identity,
and stores only the SHA-256 owner proof in the complete canonical binding
(`scripts/orchestrator.py:384-495`).

WORKSPACE -> PLAN now constructs a new durable evidence object rather than
persisting the caller mapping. It reads the fixed PLAN artifact contract from
`requirement_for()`, verifies the required artifact names are present, and then
projects only input hash, approval ID, the exact controller-required artifacts,
and the WorkspaceManager-derived task/revision/binding fields
(`scripts/orchestrator.py:563-596`). `_ledger_backed_evidence()` adds the run and
ledger-fetched approval record after projection (`scripts/orchestrator.py:721-733`).
Consequently:

- raw owner-token values in ordinary or nested aliases are discarded;
- extra artifact values and caller `required_artifacts` are replaced by the
  fixed `workspace` and `task-plan` contract;
- original receipts and receipt copies under unknown keys never enter the gate
  or transition payload;
- truthy blocking findings fail before projection and transition.

The regression tests cover all of those cases and assert both absence of the
real token and the exact durable evidence key set
(`scripts/tests/test_fake_e2e.py:426-474`). A separate temporary-state
reproduction combined ordinary, nested, extra-artifact, custom-required-artifact,
and duplicated-receipt smuggling; transition succeeded with the canonical
evidence, no raw token or unknown field was durable, and artifacts were exactly
`workspace` and `task-plan`.

A supplied `workspace_binding` must now be deeply equal to the entire derived
binding before transition (`scripts/orchestrator.py:548-555`). Exact values pass;
forged objects and near matches changing worktree identity or owner proof return
`WORKSPACE_BINDING_MISMATCH` while the run remains in WORKSPACE
(`scripts/tests/test_fake_e2e.py:476-521`). The temporary-state reproduction
independently confirmed the owner-proof near match is rejected.

The unbound compatibility path projects only safe policy evidence, promotes no
task, revisions, or workspace binding, and explicitly withholds top-level source
revisions when no bound workspace exists (`scripts/orchestrator.py:527-532,
582-596,639-645`). Both the regression and temporary reproduction confirm the
PLAN checkpoint is non-executable and `PhaseProtocol.next()` returns
`SOURCE_REVISION_REQUIRED` (`scripts/tests/test_fake_e2e.py:395-410`).

No new Critical or Important regression was identified in the round-3 files.

## Verdicts

**Specification: PASS for Fix Round 3 scope.** N1 is addressed and I2, I3, and
I5 remain addressed. I1 remains an explicit external Task 10 readiness blocker;
this scoped verdict does not represent live preflight as complete.

**Standards/Security: PASS.** G4 evidence is now an allowlisted controller-owned
projection, raw owner tokens cannot survive the reviewed smuggling paths, and
every supplied workspace-binding alias must exactly match the derived identity.

**Overall: ACCEPT FIX ROUND 3 WITH I1 OPEN.** The N1 repair is ready to retain.
Do not close overall Task 10 live readiness until confirmed profiles and an
authorized query-only six-component preflight exist.

## Verification

- Exact focused command from `/Users/tom/Desktop/skills`: 32/32 PASS.
- The run emitted the previously recorded unclosed-SQLite `ResourceWarning`
  diagnostics; it had zero failures and zero errors.
- Temporary-state reproduction: all token-smuggling aliases discarded; exact
  artifact projection retained; near-match binding rejected in WORKSPACE;
  unbound PLAN contained no executable identity and returned
  `SOURCE_REVISION_REQUIRED` from `next()`.
- Round-3 hashes matched: `orchestrator.py`
  `497030ba0ca86c75a2bb6d3b5392feb4c59ac211229ca7816a9b06542a97863a`;
  `test_fake_e2e.py`
  `76b4beab2a544fa7b82741a1b7f3901ec5ff54866109b3a2bc7d092d81f81630`.
- No live probe/write, local BGW/XFlow compile/test/integration/release command,
  or Git operation was performed.
