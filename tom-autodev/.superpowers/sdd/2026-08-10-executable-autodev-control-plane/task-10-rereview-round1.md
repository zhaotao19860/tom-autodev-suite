# Task 10 Fix Round 1 Scoped Re-review

Date: 2026-08-11

Reviewed the exact fix-round files and hashes in the scoped package. All six
reported hashes match the files on disk. This review used static inspection,
the focused fake-only suite, and temporary-state reproductions only. It made no
live request and ran no BGW/XFlow project command.

## Finding Verdicts

### I1 - OPEN (external blocker)

The report continues to state that confirmed BGW/XFlow profiles are absent and
that both attempted preflights stopped with `PROJECT_NOT_READY`, empty
components, and zero probes. It does not claim a six-component live result
(`task-10-report.md`, Fix Round 1). This is honest, but Task 10's required live
dependency check remains incomplete until profiles are confirmed and a separately
authorized query-only run checks all six components.

### I2 - NOT ADDRESSED

The parameterized BGW/XFlow scenario is a substantial improvement: it now drives
actual CafeClient snapshot normalization, CollaborationSession G0, PhaseProtocol,
KnowledgeSync, submit ownership, actual IpipeRuntime uncertainty reconciliation,
duplicate trigger/message handling, RunSummary, and G10 reject/apply
(`scripts/tests/test_fake_e2e.py:235-449`).

However, two explicitly required recovery/routing cases remain direct-state
placeholders. Test/environment/mixed routing still creates IPIPE with a raw
`state.transition()` before calling `route_failure()`
(`scripts/tests/test_fake_e2e.py:160-173`). The crash-after-external-intent case
still inserts an arbitrary KU intent directly and only calls `next()`
(`scripts/tests/test_fake_e2e.py:175-182`). The integrated trigger timeout is
reconciled inside the same runtime call (`scripts/tests/test_fake_e2e.py:337-359`);
it does not simulate a process crash leaving a pending production intent and a
fresh controller recovering or blocking it. Therefore the new path establishes
an integrated happy/uncertain-result/code-failure trace, but does not close the
brief's crash-after-intent and all-role production-boundary requirement.

### I3 - NOT ADDRESSED

Whole `summary_runtime` replacement is removed and missing runs now fail, but
`optimize()` only authenticates the caller-supplied `run_id` before dispatch
(`scripts/orchestrator.py:143-174`). `propose()` then derives ownership from the
supplied summary, and `apply()` derives it from the proposal ID; neither is
checked against the method's `run_id`. A temporary fake-only reproduction
started run A and run B, built run A's summary, then called
`optimize(run_b, "propose", summary=summary_a, ...)`; it returned `OK` with a
proposal whose `run_id` was run A.

The method also still accepts an entire caller-provided `knowledge_sync` object
(`scripts/orchestrator.py:152,160-164`). RunSummary validates the returned receipt
shape but not that it came from the controller's durable KnowledgeSync intent,
so this remains broader than a transport seam. Bind summary/proposal/approval
run identity to the method argument before every operation, and inject only
run-pinned KU/iCafe transports through the owned KnowledgeSync factory.

### I4 - ADDRESSED

Preflight now restricts project IDs, loads only the canonical project filename,
and rejects `profile.project_id != project` before probe construction or calls
(`scripts/orchestrator.py:177-208`). The regression test mutates `bgw.yaml` to
XFlow and verifies zero queries (`scripts/tests/test_live_preflight.py:169-180`).

### I5 - NOT ADDRESSED

Nested diagnostic values now use the hardened RunSummary structured redactor,
and the added Authorization/API-key/token/URL/email/phone test is useful
(`scripts/preflight.py:13,95-108`; `scripts/tests/test_live_preflight.py:150-167`).
But `_component_result()` redacts the mapping and then overwrites the cleaned
`reason_code` with the original value (`scripts/preflight.py:95-104`). Any
secret-bearing value that also matches the uppercase reason-code syntax bypasses
redaction. Fake-only reproductions returned both `TOKEN_TOP_SECRET` and
`AUTHORIZATION_BEARER_SECRET` unchanged. Use a fixed allowlist/mapping for
production reason codes, or reject secret-pattern matches before restoring the
field; add this adversarial regression.

### I6 - ADDRESSED

`trace()` now resolves the current profile through `_runtime_profile()` and
returns its fail-closed result on byte/hash drift before reading KU identity
(`scripts/orchestrator.py:210-219`). The test confirms the prior BGW-to-XFlow KU
parent drift returns `PROFILE_CONFLICT` and no project mapping
(`scripts/tests/test_fake_e2e.py:191-203`).

### I7 - ADDRESSED

The iCode adapter now closes stdin for bare login, passes no token, bounds the
returned diagnostic fields, and accepts only an `Already logged in as:` response
(`scripts/preflight.py:147-181,241-266`). Tests verify exact argv/options for all
six live adapters, `subprocess.DEVNULL`, absence of token input, and rejection of
a fresh `Logged in as:` response (`scripts/tests/test_live_preflight.py:211-301`).
Within the documented CLI constraint, this closes the originally identified
interactive-authentication path.

## New Finding

### N1 - Important - `advance()` persists caller-forged task and baseline revisions

The fix promotes any nonempty caller `task_id` and any syntactically complete
caller `source_revisions` into the committed transition event
(`scripts/orchestrator.py:342-410,1055-1060`). For WORKSPACE -> PLAN, G4 requires
only named artifacts and an approval over the caller-selected input hash
(`scripts/evidence_policy.py:22,85-115`). EvidenceGate compares
`repo_revisions` with `evidence_revisions`, but never binds either to
`source_revisions`, a durable workspace receipt, or a task DAG frontier
(`scripts/evidence_gate.py:20-22`).

A temporary fake-only reproduction approved `approved-hash`, supplied equal
`repo/evidence` revisions of `real-b/real-t`, but supplied
`task_id=FORGED-TASK` and `source_revisions=forged-b/forged-t`. `advance()`
returned `OK` and persisted both forged values in the PLAN event. PhaseProtocol
will consequently issue PLAN/IMPLEMENT actions from an unowned baseline,
regressing Task 7 revision and task ownership. Derive these fields from a
validated durable WorkspaceManager/task-frontier artifact or receipt and bind
that identity into the G4 approval hash; never promote free caller fields.

## Verdicts

**Specification: FAIL.** I2, I3, and I5 remain open, I1 remains an explicit
external blocker, and N1 violates the preserved exact task/revision contract.

**Standards/Security: FAIL.** Cross-run G10 dispatch, forgeable PLAN baselines,
and reason-code secret leakage are blocking integrity/security defects.

**Overall: REJECT FIX ROUND 1.** I4, I6, and I7 are closed. Continue with a
scoped repair for I2, I3, I5, and N1; do not represent I1 as closed.

No new Critical finding was identified.

## Verification

- Focused Task 10 suite: 20/20 PASS.
- Reported full suite: 425/425 PASS; schema 14/14 PASS; compileall PASS.
- Fake-only reproductions: cross-run G10 proposal accepted; secret-bearing valid
  reason codes leaked; caller-forged PLAN task/revisions persisted.
- Exact fix-round hashes matched the scoped review package.

The passing suite confirms the intended fixes it asserts, but currently lacks
negative coverage for the three successful reproductions above.
