# Task 5 Round 2 Re-review: Embedded Infoflow Collaboration and Approval

## Open-Finding Dispositions

### Critical C1: G0 binding must use the immutable canonical profile/card snapshot

**NOT ADDRESSED** (Specification, blocking).

The orchestrator factory is now strict: `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:344` passes `self.approvals`, and the strict branch in `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:56` through `:82` ignores caller title/roster in favor of `collaboration_binding`. Role members are copied from the validated profile at intake.

The card binding is not canonical, however. `Orchestrator.start()` does not read an iCafe/RequirementSnapshot title; it writes `{"id": requirement_id, "title": requirement_id}` at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:50` through `:53`. `CollaborationSession.prepare_g0()` then treats that placeholder as the immutable card title at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:64` through `:80`. A real `start("BGW-1", "bgw")` therefore derives `bgw-BGW-1-BGW-1-研发测试协作`, not the required group name containing the canonical card's short title. The new test avoids this production path by manually inserting `"Recorded requirement"` into a synthetic intake at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:55` through `:72`.

Concrete impact: the fix prevents caller title substitution but replaces it with a non-card placeholder, so G0 still cannot bind the required canonical group name/card snapshot.

Required correction: capture the validated immutable iCafe card snapshot, including its real title/content identity, before G0 preparation; never synthesize title from the card ID.

### Critical C2: Strict run/responder/gate binding and full gateway validation

**NOT ADDRESSED** (Specification and Standards, blocking).

The ledger/gate/CLI portion is addressed. Strict rows now reject an omitted or mismatched run at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:150` through `:161`; gate approval matching includes current `run_id` and excludes `legacy` at `/Users/tom/Desktop/skills/tom-autodev/scripts/evidence_policy.py:53` through `:65`; and the CLI requires run and responder at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:455` through `:476`.

The gateway envelope is still not fully validated. `_valid_request_envelope()` accepts any nonempty channel and any nonempty member strings at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:104` through `:111`; it does not require `channel == "infoflow"` or a supported approval channel/full-email policy. A request for `channel="codex"` or another unrequested channel is accepted when its policy has the same key, and `reply()` only checks equality with that caller-selected channel at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:68` through `:76`.

Concrete impact: the embedded approval gateway still permits a forbidden/unrequested channel to establish and resolve its own approval envelope, contrary to the exact Comate+Infoflow and fail-closed channel policy.

Required correction: enforce the gateway's exact Infoflow channel, full-email members, immutable policy snapshot, and one typed envelope schema before storing the request.

### Critical C4: Executable deadline/timeout/heartbeat/handoff lifecycle

**NOT ADDRESSED** (Specification and Standards, blocking).

The orchestrator now exposes wait/reply/heartbeat/timeout methods at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:409` through `:438`, and ledger timeout requires a StateStore for strict requests at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:87` through `:103`. These are substantive repairs.

Two state-machine gaps remain. First, the approval envelope sends `deadline_at` (`/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:366` through `:370`), but `PendingApprovalGateway.request()` ignores it and computes a separate deadline from an absent/default `timeout_seconds=1800` at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:24` through `:38`. Ledger and gateway can therefore disagree about the bound deadline. Strict requests also allow `deadline_at=None`, so `ApprovalLedger.timeout()` has no early-timeout boundary while the gateway independently assumes 30 minutes.

Second, gateway expiry detected by `wait()` or `reply()` only changes the in-memory status at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:49` through `:67`; neither path persists the required timeout handoff. Only an explicit `timeout()` call writes the handoff at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:82` through `:94`. The existing gateway test takes the unpersisted `wait()` path at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py:144` through `:153` and does not inspect recovery state.

Concrete impact: an approval can be timed out by one component while still pending in the other, and direct gateway wait/reply timeout does not create the stable recovery handoff. Heartbeat evidence is consequently attached to divergent deadline state.

Required correction: bind one canonical deadline in ledger and gateway, require it for strict requests (or apply one explicit shared default), and route every transition to `TIMEOUT` through one idempotent handoff-persisting function.

### Important I4: Boundary and state-machine regression coverage

**NOT ADDRESSED** (Standards and Specification, blocking).

The round-2 suite now covers the strict factory, omitted run, legacy gate rejection, complete-payload conflict, gateway explicit timeout, and orchestrator lifecycle. It still does not exercise a real `Orchestrator.start()` collaboration name against a canonical iCafe title, unsupported gateway channels, post-request member-policy mutation, timeout reached through gateway `wait()`/`reply()` with a durable handoff assertion, ledger/gateway deadline equality, or malformed wait/reply auditing. Those omissions directly conceal C1, C2, C4, and the new findings below.

### Important N1: Approval delivery intent must bind the complete canonical payload

**ADDRESSED** (Specification and Standards, no remaining blocking issue from N1).

`_deliver_approval_channel()` now canonicalizes the complete channel envelope and stores its SHA-256 in the durable claim at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:379` through `:405`. A retry with changed evidence produces `APPROVAL_DELIVERY_CONFLICT` before reconciliation/send. The safety test covers that changed-evidence call at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_task5_safety.py:297` through `:335`.

## Regression Confirmation

- **C3 remains ADDRESSED:** both Comate and Infoflow deliveries use durable per-channel claims, completed receipts replay locally, pending outcomes reconcile only, and strict resolution requires both receipts (`/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:348` through `:407`; `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:158` through `:161`).
- **C5 remains ADDRESSED:** group/message writes continue to use atomic `claim_intent()` and run-scoped keys (`/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:98` through `:118`, `:144` through `:164`).
- **I1 remains ADDRESSED:** auth/platform/release-rule routes use only the canonical stored owner (`/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:125` through `:130`).
- **I2 remains ADDRESSED:** Markdown mentions must exactly equal `atUsers`, and untrusted text neutralizes `@` (`/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:134` through `:142`, `:247` through `:254`).
- **I3 remains ADDRESSED:** normalized fixed project members supply the canonical owner (`/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:29` through `:53`).

## New Fix-Round Findings

### Critical N2: Gateway member authorization is mutable after request creation

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:34` copies only the outer `member_policy` dictionary; its channel member lists remain shared with the request. `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:45` and `:47` return shallow copies of the record, again exposing the same nested policy object. Mutating either the original request's list or `created["member_policy"]["infoflow"]` therefore mutates the policy held in `_requests`. `reply()` trusts that mutable list at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:72`.
- Concrete impact: after an approval request is created, a caller can append a new responder to the returned policy and make that responder authorized. The supposedly immutable G0/member approval snapshot is not immutable, so unknown members can become the first effective responder.
- Affected criteria: approval contract 2, 3, and 5; immutable member policy; authorized first-valid response.
- Required correction: validate and deep-copy/freeze the policy on input, never expose internal nested objects, and return deep copies or typed immutable views from every gateway method.

### Important N3: Malformed orchestrator replies are not rejected and audited consistently

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: `wait_infoflow_approval()` forwards only `APPROVE`/`REJECT` decisions to the ledger at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:417` through `:424`; a response with `decision="MAYBE"` falls through as `PENDING` and creates no invalid-response audit row. Direct `receive_infoflow_reply()` validates only run/approval/input/channel at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:426` through `:429`, then indexes `response["decision"]`; a missing decision raises `KeyError` instead of producing an audited fail-closed rejection.
- Concrete impact: malformed gateway responses can disappear as pending state or crash the callback path, violating the requirement that malformed decisions be rejected and auditable without changing the effective decision.
- Affected criteria: approval contract 3 and 5; malformed response auditing.
- Required correction: route every envelope with a present or missing decision through one typed ledger receive/audit API and return a stable reason code rather than branching malformed values into `PENDING` or raising.

## Dual-Axis Verdict

- Specification compliance: **FAIL**
- Code quality/standards: **FAIL**
- Round-1 open findings addressed: N1
- Round-1 open findings not addressed: C1, C2, C4, I4
- Previously addressed findings still addressed: C3, C5, I1, I2, I3
- New findings: 1 Critical (`N2`), 1 Important (`N3`)
- Gate result: **FAIL**. Blocking confirmed findings remain; Task 5 cannot pass this repair round.

## Review Basis

- Static re-review only. No tests, compilation, live setup checks, network calls, BGW/XFlow commands, or external writes were run.
- `/Users/tom/Desktop/skills/tom-autodev` is not a Git repository, so no exact diff baseline exists. The 14-file reviewed manifest SHA-256 is `ddf5a8c2a27454570712964a08dd36397c30e4fb72aab66dda6e7ee255308e84`.
- Current file hashes:
  - `scripts/state_store.py` `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
  - `scripts/collaboration.py` `fe0175239213a43abe5987d67fc0c0d37d2f30e518b0b038daa6846182d8b19b`
  - `scripts/approval_ledger.py` `3e663a3c9e7aa8d7de42f0ac3c72d477c348be5fe753e2a63e66f8cc76f0dc5a`
  - `scripts/orchestrator.py` `6206c81c4a7903c3abb88be46b6512d0afa50f161fd38686892b4f0db4d066f6`
  - `scripts/evidence_gate.py` `9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321`
  - `scripts/evidence_policy.py` `7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d`
  - `scripts/clients/infoflow_group_client.py` `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
  - `scripts/clients/infoflow_approval_client.py` `50f1351bf223e0a01b04c8518e3e4d219fee23b145bb02670d9b89bab306c76f`
  - `scripts/tests/test_task5_safety.py` `9c76c3edc7ae1da3ba115f6d3775b06c51504bb37c2a2e22f36b438408c3f1bf`
  - `scripts/tests/test_collaboration.py` `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
  - `scripts/tests/test_approval_channels.py` `8266aab004a64106606446128680354fab3d3056f4950a2f8beaf44339524983`
  - `scripts/tests/test_gates.py` `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
  - `scripts/tests/test_orchestrator.py` `ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99`
  - `infoflow-gateway/gateway.py` `004b82c5bd6a3f3c2c8afdb62a279bfd30e8b8436ec52c45e150094e8c35e360`
- No runtime `tom-autorelease` or Codex reference appears in the reviewed Task 5 production files.

## Unverifiable Items

- Exact change-set provenance is **UNVERIFIABLE** without a Git baseline; this report identifies the complete reviewed current scope by manifest hash.
- The implementer's reported `12/12`, `67/67`, `204`, compilation, and scan results are **UNVERIFIABLE in this re-review** because execution was prohibited.
- Live Comate/Infoflow callback authentication, remote reconciliation behavior, and installed group-script permissions remain **UNVERIFIABLE** under the fake-only/no-live-write constraint.

Every requested finding is classified exactly once. No provider suggestion requires `NEEDS_CLARIFICATION`.
