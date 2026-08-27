# Task 5 Round 1 Re-review: Embedded Infoflow Collaboration and Approval

## Disposition Of Original Findings

### Critical C1: G0 collaboration authorization is caller-forgeable and not bound to the canonical run/profile/card

**NOT ADDRESSED** (Specification, blocking).

The strict path is only strict when a caller constructs the session with an approval ledger: `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:23` accepts `approvals=None`, and `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:340` still constructs a session without passing `self.approvals`. With that factory path, `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:81` through `:84` continues to accept a caller-fabricated legacy `g0_approval` dictionary. A run that does have an intake event is still eligible for this fallback; the presence of a run context does not force ledger authorization.

Even the ledger-backed `prepare_g0()` path reads only the first intake event's project, requirement ID, and profile hash at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:156` through `:164`. It does not load the validated profile/member snapshot or a canonical card snapshot/title from durable run state. A caller can therefore prepare a different title/member set under the same requirement ID and obtain an approval for that caller-provided data. The approval is not bound to the validated profile content.

The new safety test passes `approvals=ledger` directly at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:60`; it does not exercise `Orchestrator.collaboration_session()` or a strict session with a run intake but no injected ledger.

Required correction: have the orchestrator always inject its durable ledger into the collaboration adapter, remove or isolate the legacy constructor so it cannot operate on a run-bound session, and derive the full profile/card/member binding from the immutable run snapshot before accepting a G0 approval.

### Critical C2: Approval identities are run-bound in storage, but strict response authorization still fails open and legacy approvals can satisfy gates

**NOT ADDRESSED** (Specification and Standards, blocking).

The schema/index repair is present: `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:22` stores `run_id` and `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:135` creates a `(run_id, action, input_hash)` identity. However, `_reject()` checks run ownership only when the caller supplies `run_id` at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:149`. `ApprovalLedger.receive()` and `Orchestrator.approve()` both make it optional (`/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:59`, `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:328`), and the CLI still calls approve without one at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:418` through `:437`. A strict row with a valid responder can therefore be resolved by a response that carries no run identity.

Legacy compatibility also remains an authorization path for phase gates. `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:247` through `:257` copies any ledger record into gate evidence, while `/Users/tom/Desktop/skills/tom-autodev/scripts/evidence_policy.py:53` through `:63` validates only approval ID, action, effective decision, and input hash. A `run_id='legacy'` approval created through the old call shape can thus be supplied to a different run's `advance()` call and satisfy the gate. The compatibility tests at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:259` and `:330` continue to exercise this unbound shape.

The embedded gateway remains independently unbound: `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:23` stores no run identity, and `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:52` validates neither approval ID nor channel/member identity. The safety test checks wrong-run rejection only when an explicit `run_id` is supplied at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:136` through `:143`.

Required correction: require `run_id` for every strict response/gate lookup, reject a legacy row whenever the caller is advancing a run-bound operation, bind gate evidence to the current run, and make gateway request/reply envelopes carry and validate the same run/approval/channel/member/input tuple.

### Critical C3: Approval delivery is dual-channel and intent-protected

**ADDRESSED** (Specification, no remaining blocking finding in the reviewed path).

`request_infoflow_approval()` now requires both clients and sends one identical payload to Comate and Infoflow at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:344` through `:373`. Each channel claims a durable intent before the external request at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:375` through `:399`; completed receipts replay locally, and an existing pending intent performs query/reconcile only. `ApprovalLedger.record_delivery()` persists channel and payload hash at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:104` through `:113`, and strict resolution refuses incomplete channel delivery at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:156` through `:160`. The focused safety test covers identical calls, both receipts, and no pending intents at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:172` through `:204`.

This disposition is limited to the normal successful/unknown-result intent flow. The new payload identity gap is recorded separately below.

### Critical C4: Deadline, timeout, late-response, heartbeat, and handoff state machine

**NOT ADDRESSED** (Specification and Standards, blocking).

`ApprovalLedger.receive()` now atomically converts a pending request to `TIMEOUT` when the supplied observation is at or after its parsed deadline, records a late response, and creates a durable handoff when a `state_store` is supplied (`/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:59` through `:85`). `timeout()` rejects early calls, preserves a resolved decision, and persists a handoff in the normal strict caller path (`/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:87` through `:102`). These are real improvements and the safety test covers them at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:145` through `:168`.

The required behavior is still not executable end to end. The orchestrator exposes no wait, timeout, heartbeat, or gateway-reply method; its only approval method remains `approve()` (`/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:328` through `:338`). `InfoflowApprovalClient.wait()` exists but has no production caller (`/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_approval_client.py:20`), and `PendingApprovalGateway.timeout()` only mutates an in-memory record and creates no durable handoff (`/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:70` through `:81`). Direct callers may also invoke ledger timeout without a `state_store`, producing a `TIMEOUT` decision with no recovery handoff (`/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:99` through `:102`). Heartbeat is likewise only a ledger helper and is not wired into the external approval lifecycle.

Required correction: expose and wire a durable orchestrator wait/reply/heartbeat/timeout path, require the state store for strict timeout handoff, and make the gateway envelope and timeout result run-bound and durable.

### Critical C5: Group/message intent ownership and one-per-run idempotency

**ADDRESSED** (Specification and Standards, no remaining blocking finding in the reviewed path).

Group creation now uses the run-scoped key `infoflow.group.create:{run_id}` at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:87` and atomically claims it via `/Users/tom/Desktop/skills/tom-autodev/scripts/state_store.py:223` through `:259`. A changed payload is a conflict, the non-owner uses reconciliation only, and the owner alone calls the external client at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:89` through `:108`. Messages use the same claim protocol at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:134` through `:154`. The concurrent/restart safety test checks one group and one message write at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:74` through `:104`.

This pass does not extend to the legacy `StateStore.intent()` API, which remains intentionally compatible for older tasks; the reviewed Task 5 write paths use `claim_intent()`.

### Important I1: Project-owner routing

**ADDRESSED** (Specification, no remaining blocking finding in the reviewed path).

`resolve_members()` retains the normalized fixed project owner separately at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:49` through `:53`, and auth/platform/release-rule failures route only to `session["owner"]` at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:115` through `:120`. The safety test verifies owner-only routing at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:106` through `:124`.

### Important I2: Markdown and `atUsers` exact-set validation

**ADDRESSED** (Specification and Standards, no remaining blocking finding in the reviewed path).

`send_message()` now requires exact equality between parsed full-email mentions and the normalized recipient set at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:124` through `:133`. `_failure_markdown()` neutralizes `@` in all untrusted summary/evidence/action fields at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:216` through `:223`. The safety test covers both routed untrusted text and an explicit extra mention at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:119` through `:124`.

### Important I3: Member-object normalization and canonical owner

**ADDRESSED** (Standards and Specification, no remaining blocking finding in the reviewed path).

The owner is selected from the normalized fixed project role at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:32` through `:53`, so dictionary member entries no longer become the owner object. Fixed profile roles are required before iCafe/card-owner augmentation at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:32` through `:48`. The safety roster intentionally uses dictionary member entries at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:42` through `:50`.

### Important I4: Required boundary and state-machine test coverage

**NOT ADDRESSED** (Standards and Specification, blocking).

`test_task5_safety.py` adds meaningful coverage for strict G0, claims, routing, dual delivery, and direct ledger deadlines. It still does not test the production `Orchestrator.collaboration_session()` factory (which is the unbound path behind C1), an omitted run ID resolving a strict approval, a legacy approval authorizing a strict `advance()`, the gateway's unbound reply/request envelope, durable timeout through an orchestrator wait path, or failure/reconciliation after a changed delivery payload. `InfoflowApprovalClient.wait()` remains uncalled by production code, and no test exercises it through an orchestrated approval request. The new tests therefore do not prove the strict/legacy boundary or the claimed executable gateway lifecycle.

Required correction: add regression tests for those exact calls and make them fail closed before marking the review pass.

## New Fix-Round Findings

### Important N1: Approval delivery intent does not bind the complete canonical delivery payload

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: the actual external payload includes `approval_id`, `run_id`, `action`, `input_hash`, `deadline_at`, and arbitrary evidence at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:362` through `:365`. The durable intent payload passed to `claim_intent()` is only `{"approval_id": approval_id, "payload_hash": payload_hash}` at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:378` through `:379`. Consequently, a retry using changed evidence under the same approval/channel key is treated as the same intent; if the first result is unknown, reconciliation receives the changed payload at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:387`, and if a receipt is complete the changed evidence is silently ignored at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:382` through `:384`.
- Concrete impact: the durable idempotency record cannot prove that both channels were sent the same evidence summary, and an unknown-result query may target a different request representation. This weakens the no-replay/query-only guarantee and makes delivery audit data inconsistent with the actual payload.
- Affected criteria: approval contract 1 and 3; exact shared canonical approval payload; external write intent/receipt identity.
- Required correction: persist a canonical, secret-safe hash of the complete outbound payload (or the full allowlisted envelope) in the intent and reject changed evidence as a conflict before any reconcile/send.

## Dual-Axis Verdict

- Specification compliance: **FAIL**
- Code quality/standards: **FAIL**
- Original findings addressed: C3, C5, I1, I2, I3
- Original findings not addressed: C1, C2, C4, I4
- New findings: 1 Important (`N1`)
- Gate result: **FAIL**. Blocking confirmed findings remain; this fix round cannot proceed to a passing Task 5 review.

## Review Basis And Evidence

- Review mode: static re-review of the requested current fix scope. No tests, compilation, live setup checks, network calls, BGW/XFlow commands, or external writes were run.
- The workspace is not a Git repository (`git rev-parse --show-toplevel` failed), so no exact diff baseline exists. The current reviewed scope is identified by the following manifest SHA-256: `034c609fd12036b017c1af2d4f0ff7191893676dc271b767e745295d9b3fe059`.
- Current file hashes:
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/state_store.py`: `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py`: `e206f66e2258c25d2efa71fd4c6a7db223bd69b03b57ad417d14b36c0737b254`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py`: `b24cb4a70b780ba56e7f2098f4bc9a2d7fc85aae7364b125fa1157ff5d7d8890`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py`: `bc994f6230356b76e662146e3b0ceaeec5e66f322a22234baa7f77b72b3a994d`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_group_client.py`: `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_approval_client.py`: `75beceb6ec05e22cb4f56973ebc80c95c7bd2c640266936c7349cf9b6faf2ba2`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py`: `283a075b474ed9d4a8e9755db73c7524ce6aa0589e6bfdc77b60ba8f908782b6`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py`: `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py`: `aad6811380f247882d7712d47b4d18822e79b814b07c20de9813172d7095e577`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_gates.py`: `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py`: `0711f6af94d29ac5cb0f69bde7d832ae76de6407d60240edb46db654bef48550`
  - `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py`: `30870dbfda305da4f14d4328e183a913afb15748406691f059c940475dea97f3`
- The removed `infoflow-gateway/README.md` is absent as requested. No runtime `tom-autorelease` or Codex reference appears in the reviewed Task 5 production files.

## Unverifiable Items

- Exact change-set provenance remains **UNVERIFIABLE** without a Git baseline; this report assesses the complete current fix scope and records its manifest hash.
- The implementer's reported `7/7`, `61/61`, `198`, compile, and scan results are **UNVERIFIABLE in this re-review** because test/compile execution was prohibited. They are treated as supplied evidence only.
- Live Comate/Infoflow transport semantics, callback authentication, remote reconciliation behavior, and installed group-script permissions remain **UNVERIFIABLE** under the fake-only/no-live-write constraint.

All original findings are classified exactly once as `ADDRESSED` or `NOT ADDRESSED`; the only new fix-round issue recorded above is `CONFIRMED` N1. No provider suggestion requires `NEEDS_CLARIFICATION`.
