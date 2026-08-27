# Task 10 Fix Round 2 Scoped Re-review

Date: 2026-08-11

Reviewed the exact round-2 package and files. All six reported hashes match the
files on disk. This review used static inspection, the focused fake-only suite,
and one temporary-state adversarial reproduction. It made no live request, ran
no BGW/XFlow project command, and performed no Git operation.

## Finding Verdicts

### I1 - OPEN (external blocker)

`/Users/tom/.tom-autodev/config/projects` remains absent. The report correctly
states that no six-component live preflight was completed and does not fabricate
component results (`task-10-report.md`, Fix Round 2). This remains an external
readiness blocker until confirmed BGW/XFlow profiles exist and a separately
authorized query-only preflight is run.

### I2 - ADDRESSED

Test, environment, and mixed failures now enter through `_integrated_run()` and
exercise the production controller boundaries before `route_failure()` and the
collaboration router; the asserted recipients are test, test, and development
plus test respectively (`scripts/tests/test_fake_e2e.py:300-320,524-724`).

The crash scenario creates a real production `ipipe.trigger` intent, exposes the
exact remote build, and raises `SystemExit` before receipt closure. A fresh
controller blocks with `RECOVERY_REQUIRED`; a fresh runtime reconciles the exact
pipeline/module/full revision map/parameters, closes the original intent, and
replays its receipt without another trigger (`scripts/tests/test_fake_e2e.py:620-663`;
`scripts/clients/ipipe_runtime.py:91-173,614-629`). Zero or multiple exact matches
remain fail-closed (`scripts/clients/ipipe_runtime.py:71-77`).

### I3 - ADDRESSED

`optimize()` rejects a summary, proposal, or approval owned by another run before
dispatching the operation (`scripts/orchestrator.py:144-186`). It no longer
accepts a caller-supplied summary runtime or KnowledgeSync object. The private
G10 construction seam accepts only KU/iCafe transports, username, and the iCafe
preflight option; the controller constructs a real pinned KnowledgeSync and then
checks state-store identity, run, card, and fixed KU parent ownership
(`scripts/orchestrator.py:293-382`). The cross-run negative tests cover both
propose and apply (`scripts/tests/test_fake_e2e.py:425-478`).

### I5 - ADDRESSED

Preflight output now restores only reason codes in the fixed allowlist. Any
unknown failure reason becomes `PREFLIGHT_QUERY_FAILED`, including uppercase
secret-shaped values; unknown success values become `OK`
(`scripts/preflight.py:16-44,122-146`). The regression explicitly covers
`TOKEN_TOP_SECRET` and `AUTHORIZATION_BEARER_SECRET`
(`scripts/tests/test_live_preflight.py:169-185`).

### N1 - NOT ADDRESSED

The controller-owned binding itself is substantially repaired. It derives the
task from `PhaseProtocol.next(WORKSPACE)`, requires exact business/test receipt
roles, matches module and repository path to the pinned profile, and verifies
durable run/task/token ownership, ACTIVE worktree registration, worktree path,
and baseline revision through `WorkspaceManager.query_ownership()`
(`scripts/orchestrator.py:384-477`; `scripts/workspace_manager.py:77-161`). The G4
hash covers the complete canonical binding, and the unbound compatibility path
cannot issue an executable PLAN action (`scripts/orchestrator.py:479-495,512-598`;
`scripts/tests/test_fake_e2e.py:394-409`).

Two required adversarial properties remain open:

- Raw owner-token non-persistence is not enforced by an evidence allowlist.
  `advance()` copies the complete caller evidence, removes only the exact
  `workspace_receipts` key, and persists all other fields in `evidence`
  (`scripts/orchestrator.py:510-548,572-598`). The global persistence policy
  rejects secret-shaped keys but does not inspect opaque string values
  (`scripts/persistence_policy.py:20-22,44-54`). A temporary fake-only
  reproduction supplied the real owner token as `opaque_proof`; the transition
  returned `OK` and the raw token was present in the durable PLAN event. The
  existing positive test checks only leakage through the removed receipt field
  (`scripts/tests/test_fake_e2e.py:349-372`).
- Caller `workspace_binding` is treated as an alias when receipts are absent,
  but with receipts present only task and revision aliases are compared. A
  conflicting supplied binding is silently overwritten rather than rejected
  (`scripts/orchestrator.py:514-547`). The same reproduction supplied
  `{"forged": true}` and still returned `OK`. The persisted binding was canonical,
  so this does not forge execution identity, but it fails the scoped alias
  mismatch rejection contract.

N1 therefore remains Important and open. No separate new Critical or Important
regression was identified in the round-2 files.

## Verdicts

**Specification: FAIL.** I2, I3, and I5 are addressed, but N1 is not fully
addressed and I1 remains the declared external blocker.

**Standards/Security: FAIL.** Canonical workspace ownership is verified, but a
caller-controlled opaque evidence field can still persist the raw owner token.
The missing `workspace_binding` alias rejection also leaves the documented input
contract incomplete.

**Overall: REJECT FIX ROUND 2.** Repair N1 with a canonical/allowlisted G4
evidence projection and explicit equality rejection for every accepted alias,
then add both adversarial regressions. Preserve I1 as OPEN.

## Verification

- Exact focused command from `/Users/tom/Desktop/skills`: 28/28 PASS.
- The focused run emitted the previously reported unclosed-SQLite
  `ResourceWarning` diagnostics; it had zero failures and zero errors.
- Temporary-state reproduction: transition `OK`; raw token persisted under
  `opaque_proof`; conflicting `workspace_binding` was not rejected and was
  overwritten by the canonical binding.
- All six round-2 file hashes matched the scoped package.
- No live probe/write, local BGW/XFlow compile/test/integration/release command,
  or Git operation was performed.
