# Task 5 Round 4 Re-review: Embedded Infoflow Collaboration and Approval

## Verdict

- Specification compliance: **FAIL**
- Standards/code quality: **FAIL**
- Addressed in round 4: `C1`, `C4`, `N4`, `N5`
- Still not addressed: `I4`
- Previously addressed findings that remain addressed: `C2`, `C5`, `I1`, `I2`, `I3`, `N1`, `N2`, `N3`
- Previously addressed finding regressed: `C3`
- New finding: Critical `N6`
- Gate result: **FAIL**. A blocking delivery-binding defect remains.

This was a static, scoped re-review. No tests, compilation, live setup checks, network calls, or external writes were run.

## Finding Classification

| Finding | Classification | Axis | Blocking | Disposition |
| --- | --- | --- | --- | --- |
| C1 | `CONFIRMED` | Specification | No remaining issue | Addressed |
| C4 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| I4 | `CONFIRMED` | Specification and Standards | Yes | Not addressed |
| N4 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| N5 | `CONFIRMED` | Specification | No remaining issue | Addressed |
| N6 | `CONFIRMED` | Specification and Standards | Yes | New Critical |

No scoped finding is `REJECTED_WITH_REASON` or `NEEDS_CLARIFICATION`.

## Round-Three Finding Dispositions

### Critical C1: Canonical RequirementSnapshot binding

**ADDRESSED** (Specification, no remaining blocking issue from C1).

`CafeClient.snapshot()` and `Orchestrator.start()` now share `requirement_snapshot.content_hash()` (`scripts/clients/icafe_client.py:48-52`, `:101-136`; `scripts/orchestrator.py:43-55`). The canonicalizer removes only the top-level `content_hash`, serializes the remaining complete snapshot with the same UTF-8/sorted-key/compact-JSON rules, and hashes that unsigned representation, so the hash does not include itself (`scripts/requirement_snapshot.py:47-50`).

`validation_error()` requires all fields emitted by the production CafeClient snapshot, checks the requested canonical card ID and complete field shapes, validates lowercase SHA-256, and recomputes the hash before intake persistence (`scripts/requirement_snapshot.py:9-27`, `:53-91`; `scripts/orchestrator.py:43-86`). Missing, partial, identity-mismatched, or title/content/hash-mismatched snapshots therefore return a structured error before creating a run. A run without a snapshot remains unable to prepare strict G0. The production-shaped fixture and negative partial/hash-mismatch cases cover this path (`scripts/tests/test_task5_safety.py:57-81`, `:104-153`).

The snapshot helper accepts additional signed fields because required fields are tested as a subset, but every such field is included in the canonical hash. That does not permit an unsigned title/content substitution or partial CafeClient result and is not a remaining Critical/Important C1 defect.

### Critical C4: Executable deadline, reply, timeout, and handoff lifecycle

**ADDRESSED** (Specification and Standards, no remaining blocking issue from C4).

The gateway now emits a complete record for every request, wait, reply, heartbeat, and timeout state and validates that record before returning it (`infoflow-gateway/gateway.py:23-126`, `:164-168`). The shared parser requires the exact public field set, exact Infoflow channel/full member policy, state-consistent nested reply or handoff, and deep-copies accepted results (`scripts/approval_contract.py:11-27`, `:60-97`, `:100-134`). The client rejects partial request/wait records (`scripts/clients/infoflow_approval_client.py:14-35`).

The orchestrator binds wait results to request ID, run, approval, channel, input hash, stored member policy, and stored deadline before any state transition (`scripts/orchestrator.py:431-466`). Full pending results are inert and unaudited; accepted nested replies are converted to the ledger envelope and deduplicated by stable `reply_id` (`scripts/orchestrator.py:469-499`). Gateway and ledger timeout paths use the same stable handoff identity/payload, and repeated orchestrator polls reconstruct the same handoff for an already timed-out ledger row (`infoflow-gateway/gateway.py:112-126`; `scripts/approval_ledger.py:63-107`; `scripts/orchestrator.py:520-542`; `scripts/state_store.py:362-387`).

The actual `PendingApprovalGateway -> InfoflowApprovalClient -> Orchestrator` tests cover pending, accepted-once, timeout reuse, and malformed wait records (`scripts/tests/test_task5_safety.py:328-468`). `N6` concerns request-delivery receipt binding before this validated wait lifecycle, not the corrected lifecycle itself.

### Important I4: Production-boundary and fail-closed regression coverage

**NOT ADDRESSED** (Specification and Standards, blocking).

Round four adds strong production-shaped coverage for canonical snapshot hashing, the actual three-component pending/accepted/timeout chain, stable handoff reuse, partial result rejection, malformed records, wait-time policy mismatch, and the exact ten-hour default (`scripts/tests/test_task5_safety.py:104-153`, `:328-568`; `scripts/tests/test_approval_channels.py:17-79`). These tests directly address the round-three omissions.

The delivery boundary still lacks the regression needed for `N6`. `test_infoflow_client_rejects_a_partial_gateway_request_result()` rejects a one-field result, but no test returns a *complete typed PENDING result* whose run, approval ID, input hash, member policy, or deadline differs from the outbound request (`scripts/tests/test_approval_channels.py:17-79`). The changed-policy test exercises `wait()` after the receipt has already been recorded, not `InfoflowApprovalClient.request()` or strict delivery activation (`scripts/tests/test_task5_safety.py:470-531`).

Concrete impact: the focused suite can remain green while a mismatched Infoflow request receipt satisfies dual-channel delivery and permits a Comate response to become effective.

### Critical N4: Shared gateway/client/orchestrator result schema

**ADDRESSED** (Specification and Standards, no remaining blocking issue from N4).

`ApprovalGatewayResult`, its reply/handoff types, and `parse_gateway_result()` define one complete public schema (`scripts/approval_contract.py:31-97`). The gateway returns that schema, the client validates it, and the orchestrator consumes its nested reply rather than looking for top-level decision fields (`infoflow-gateway/gateway.py:23-126`, `:164-168`; `scripts/clients/infoflow_approval_client.py:14-35`; `scripts/orchestrator.py:431-499`). Actual-chain tests confirm that a full pending record creates no audit, a nested accepted reply is applied once, and malformed/partial records fail closed.

### Important N5: Exact ten-hour default

**ADDRESSED** (Specification, no remaining blocking issue from N5).

The sole strict default is now `36000` seconds in the shared approval contract and the ledger imports it (`scripts/approval_contract.py:8`; `scripts/approval_ledger.py:10`, `:39-48`). The fixed-clock regression asserts `2026-08-11T10:00:00+00:00` in both the ledger and gateway (`scripts/tests/test_task5_safety.py:533-568`), matching `references/approval-policy.md:16`.

## New Fix-Round Finding

### Critical N6: A complete but mismatched Infoflow request receipt is accepted as delivery for the local approval

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Evidence: `InfoflowApprovalClient.request()` retains the outbound `gateway_request`, parses a complete `PENDING` result, but checks only `status == "PENDING"`; it never compares the returned run, approval ID, channel, input hash, member policy, or deadline with the request (`scripts/clients/infoflow_approval_client.py:14-24`). `Orchestrator._deliver_approval_channel()` then persists that returned record and calls `record_delivery()` with the *local* input hash supplied separately (`scripts/orchestrator.py:401-429`). `ApprovalLedger._reject()` considers a channel delivered solely because the stored delivery entry carries that local `payload_hash` (`scripts/approval_ledger.py:186-189`).
- Affected criteria: approval contracts 2, 3, and 5; identical dual-channel publication; delivery receipt integrity; mismatched approval/run/input/policy rejection.
- Concrete impact: an injected or misrouted Infoflow transport can return a fully typed PENDING record for another run/approval/input/policy/deadline. That response is recorded as successful delivery for the local approval. A valid Comate responder can then become the first effective decision even though Infoflow never acknowledged the local envelope. A later wait may reject the mismatch, but the approval may already be effective.
- Required correction: in `InfoflowApprovalClient.request()`, bind the parsed result to every immutable outbound field (`run_id`, `approval_id`, `channel`, `input_hash`, canonical member policy, and normalized deadline) before returning it. Alternatively enforce the comparison in `_deliver_approval_channel()` before writing the receipt. Add complete-result mismatch tests for each identity field and assert that no Infoflow delivery receipt is recorded.

This finding also means original `C3` no longer remains fully addressed: durable intent ownership and payload hashing still work, but strict dual delivery can be activated by a receipt that is not bound to the outbound Infoflow envelope.

## Previously Addressed Findings

- **C2 remains addressed:** strict ledger responses and gates are run-bound; the gateway enforces exact Infoflow, full-email Comate+Infoflow policy, responder, approval, run, and input binding. `N6` is the separate request-result receipt binding gap.
- **C5 remains addressed:** group/message writes retain run-scoped atomic intent ownership, reconciliation-only unknown recovery, and payload conflict checks (`scripts/collaboration.py:86-166`).
- **I1 remains addressed:** owner-only failures target the canonical project owner (`scripts/collaboration.py:122-134`).
- **I2 remains addressed:** Markdown mentions exactly equal `at_users`, and untrusted fields neutralize `@` (`scripts/collaboration.py:136-166`, `:250-259`).
- **I3 remains addressed:** normalized fixed project members provide the canonical owner (`scripts/collaboration.py:29-54`).
- **N1 remains addressed:** delivery intents bind the SHA-256 of the complete outbound envelope and changed evidence/member/deadline payloads conflict before send/reconcile (`scripts/orchestrator.py:401-429`).
- **N2 remains addressed:** gateway request input and every public nested result are deep-copied (`infoflow-gateway/gateway.py:33-55`; `scripts/approval_contract.py:97`).
- **N3 remains addressed:** missing/malformed decisions and malformed wait results are audited fail-closed without changing the effective decision (`scripts/orchestrator.py:439-466`, `:501-512`; `scripts/approval_ledger.py:127-149`, `:178-192`).

## Review Basis

- Reviewed manifest SHA-256: `d8491fb147f4b8626afa4b1642dbaa4662ad641a7663731b45ba53ff946e8e84`
- Task brief SHA-256: `616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db`
- Round-3 re-review SHA-256: `0d96459ac9c9a849b192a844936166ed6617c9257684ab258359ec8a7143c999`
- Implementation report with round-4 appendix SHA-256: `630bae6ffc53462c16591e75405dbd6a493f743aab815e60f73f31f6ca64f3bf`
- Approval policy SHA-256: `ebf5950a883fdb1c61c69fcabf4a1cf42422be7b6efb6b40befff208817e0d13`
- External contracts SHA-256: `b7522ca349add10e3d2341f9c2be59593f9a08c85425b8b26a045939262cc9b6`

Current reviewed file hashes:

- `scripts/state_store.py` `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
- `scripts/requirement_snapshot.py` `fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9`
- `scripts/approval_contract.py` `97019bb2dcb5f35c74589b2fe8d09935356502c7856f310a898834db540615be`
- `scripts/collaboration.py` `148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825`
- `scripts/approval_ledger.py` `bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10`
- `scripts/orchestrator.py` `1f33021e3289751dcf8ce41546f8fc38bf1d9e1eaf27b5c70457d3353ef0cb44`
- `scripts/evidence_gate.py` `9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321`
- `scripts/evidence_policy.py` `7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d`
- `scripts/clients/icafe_client.py` `886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1`
- `scripts/clients/infoflow_group_client.py` `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
- `scripts/clients/infoflow_approval_client.py` `cee73c79452a6a4ab68ce4391302bb6bc4e31779771292401718836dec9b3c00`
- `scripts/tests/test_task5_safety.py` `b6acdd19115465d0d1689ef2433b69a65804e29d262acc792cd860e48c209411`
- `scripts/tests/test_collaboration.py` `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
- `scripts/tests/test_approval_channels.py` `87bfa85bd1b22b8ed183cfd834ae3f12a3de1657bd11b95a78b3479df623da9e`
- `scripts/tests/test_gates.py` `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
- `scripts/tests/test_orchestrator.py` `ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99`
- `infoflow-gateway/gateway.py` `de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0`

The workspace is not a Git repository, so exact change-set provenance remains **UNVERIFIABLE**. The manifest and individual hashes identify the complete reviewed current scope. No runtime `tom-autorelease` or Codex reference appears in the reviewed Task 5 production files.

## Unverifiable Items

- The implementer's reported `76/76`, `213/213`, compilation, and scan results are **UNVERIFIABLE in this re-review** because execution was prohibited.
- Live Comate/Infoflow callback authentication, remote reconciliation behavior, credential configuration, deployment import paths, and installed group-script permissions remain **UNVERIFIABLE** under the fake-only/no-live-write constraint.
- Human confirmation/provenance of a supplied RequirementSnapshot remains outside the reviewed dictionary/hash boundary.
