# Task 5 Round 5 Re-review: Embedded Infoflow Collaboration and Approval

## Verdict

- Specification compliance: **FAIL**
- Standards/code quality: **FAIL**
- `N6`: **ADDRESSED**
- `C3`: **NOT ADDRESSED**
- `I4`: **NOT ADDRESSED**
- All other previously addressed findings remain addressed: `C1`, `C2`, `C4`, `C5`, `I1`, `I2`, `I3`, `N1`, `N2`, `N3`, `N4`, `N5`
- New finding: Critical `N7`
- Gate result: **FAIL**. A definitive invalid Infoflow result can still be persisted as successful delivery.

This was a static, scoped breaker re-review. No tests, compilation, live setup checks, network calls, or external writes were run.

## Finding Classification

| Finding | Classification | Axis | Blocking | Disposition |
| --- | --- | --- | --- | --- |
| N6 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| C3 | `CONFIRMED` | Specification and Standards | Yes | Not addressed |
| I4 | `CONFIRMED` | Specification and Standards | Yes | Not addressed |
| N7 | `CONFIRMED` | Specification and Standards | Yes | New Critical |

No scoped finding is `REJECTED_WITH_REASON` or `NEEDS_CLARIFICATION`.

## Round-Four Finding Dispositions

### Critical N6: Bind a complete Infoflow request result to the outbound envelope

**ADDRESSED** (Specification and Standards, no remaining issue from the six immutable-field mismatch).

`approval_contract.gateway_result_matches_request()` now compares exact `run_id`, `approval_id`, `channel`, and `input_hash`, canonicalizes both complete Comate+Infoflow policies by sorting each member list, and parses both timezone-aware deadlines to the same UTC representation (`scripts/approval_contract.py:101-148`). This accepts order-only policy differences and equivalent deadline offsets while rejecting actual binding differences.

`InfoflowApprovalClient.request()` requires a complete parsed `PENDING` result and applies the shared six-field matcher before returning (`scripts/clients/infoflow_approval_client.py:18-32`). `Orchestrator._deliver_approval_channel()` independently parses and matches the raw returned result before either StateStore receipt persistence or `ApprovalLedger.record_delivery()` (`scripts/orchestrator.py:401-452`). The outbound envelope uses the ledger-canonical member policy rather than the caller's original ordering/duplicates (`scripts/orchestrator.py:384-396`).

The client tests vary all six immutable fields individually and include a matching reversed-policy/equivalent-deadline success (`scripts/tests/test_approval_channels.py:81-175`). The orchestrator tests invoke every mismatch twice and assert a stable `APPROVAL_REQUEST_RESPONSE_INVALID`, one send, zero reconcile calls, no Infoflow ledger receipt, pending/unresolved approval, and a blocked Comate response (`scripts/tests/test_task5_safety.py:348-454`). A raw typed client bypass test confirms the orchestrator-side check independently (`scripts/tests/test_task5_safety.py:456-509`).

Deterministic binding failures are saved under the delivery failure key before returning. On an identical retry, `_deliver_approval_channel()` reads that result before completed-receipt or existing-intent reconciliation, so it neither resends nor reconciles (`scripts/orchestrator.py:404-438`). Changed outbound payloads still conflict at `claim_intent()` before the failure replay lookup, preserving `N1`.

### Critical C3: Dual-channel delivery and durable failure recovery

**NOT ADDRESSED** (Specification and Standards, blocking).

The round-five N6 path is repaired: a structurally complete but six-field-mismatched result cannot satisfy Infoflow delivery, deterministic mismatch failure is durable/stable, Comate cannot authorize with only its own receipt, and matching normalized results succeed.

However, the typed public schema permits a `PENDING` result with a non-null `reason_code`. This is how the gateway represents a definitive rejected operation while leaving the stored request pending: invalid reply paths return a copied pending record plus `APPROVAL_ENVELOPE_INVALID`, `APPROVAL_INPUT_MISMATCH`, `APPROVAL_RESPONDER_UNAUTHORIZED`, or `APPROVAL_DECISION_INVALID` (`infoflow-gateway/gateway.py:66-81`; `scripts/approval_contract.py:78-98`). Neither request boundary requires `reason_code is None`: the client checks only parsed shape, `status == "PENDING"`, and six-field equality (`scripts/clients/infoflow_approval_client.py:25-31`), and the orchestrator repeats the same three checks (`scripts/orchestrator.py:439-449`).

Consequently, a complete, correctly bound `PENDING` result carrying a definitive gateway error is receipted at `scripts/orchestrator.py:450-451` and counted as Infoflow delivery by `ApprovalLedger._reject()` because the locally supplied payload hash is present (`scripts/approval_ledger.py:109-118`, `:186-189`). A valid Comate responder can then become effective even though Infoflow explicitly reported an invalid request/result.

Required correction: define the valid initial request-result state as `status == "PENDING"`, `reason_code is None`, `reply is None`, and `handoff is None` in one shared predicate used by both client and orchestrator. A non-null request-result reason must take the same durable stable-failure path as binding mismatch, with no Infoflow ledger receipt and no authorization.

### Important I4: Complete request-boundary regression coverage

**NOT ADDRESSED** (Specification and Standards, blocking).

Round five now has the requested six mismatch subtests, normalized matching success, duplicate retry/no-reconcile assertions, raw-client orchestrator recheck, no Infoflow receipt, unresolved state, and Comate delivery-incomplete assertion. This closes the specific N6 coverage omission.

The suite does not test a complete, six-field-matching `PENDING` request result with a non-null `reason_code`. Every request fixture used by the new client/orchestrator tests sets `reason_code` to `None` (`scripts/tests/test_approval_channels.py:107-127`, `:137-158`; `scripts/tests/test_task5_safety.py:379-400`, `:464-482`). Therefore the reported focused/full suite can remain green while `N7` authorizes a definitively invalid Infoflow result.

Required correction: add client and raw-orchestrator tests for at least each gateway-produced pending error code, repeat the same request, and assert stable `APPROVAL_REQUEST_RESPONSE_INVALID`, one transport call, zero reconcile calls, no Infoflow receipt, unresolved approval, and `APPROVAL_DELIVERY_INCOMPLETE` for Comate.

## New Fix-Round Finding

### Critical N7: A correctly bound PENDING result with a gateway error is recorded as successful delivery

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Evidence: `parse_gateway_result()` allows a string `reason_code` on `PENDING` (`scripts/approval_contract.py:78-98`). `InfoflowApprovalClient.request()` and the orchestrator pre-receipt check omit `reason_code is None` (`scripts/clients/infoflow_approval_client.py:25-31`; `scripts/orchestrator.py:439-449`). The gateway itself emits pending/error records for definitive invalid envelopes, inputs, responders, and decisions (`infoflow-gateway/gateway.py:66-81`).
- Affected criteria: approval contracts 2, 3, and 5; explicit delivery failure; dual-channel receipt integrity; fail-closed malformed/mismatched result handling.
- Concrete impact: a stale, misrouted, or boundary transport can return a complete matching pending record with `reason_code="APPROVAL_ENVELOPE_INVALID"`. The controller records an Infoflow delivery receipt and a later Comate approval becomes effective. The explicit Infoflow rejection is converted into successful dual delivery.
- Required correction: centralize an initial-request success predicate that requires a clean pending state, apply it at both boundaries, persist non-clean results as deterministic delivery failures, and cover retry/no-receipt/no-authorization behavior.

## Previously Addressed Findings

- **C1 remains addressed:** CafeClient and start share complete non-circular RequirementSnapshot hashing and fail before intake on partial/identity/hash mismatch.
- **C2 remains addressed:** exact Infoflow channel, strict run/input/responder/member policy, dual configured channels, and run-bound gates remain enforced. `N7` concerns delivery-result state, not those identity checks.
- **C4 remains addressed:** the actual gateway/client/orchestrator wait lifecycle handles full pending, nested accepted-once, timeout, malformed audit, and stable handoff state.
- **C5 remains addressed:** group/message writes retain atomic run-scoped ownership and query-only unknown recovery.
- **I1-I3 remain addressed:** canonical owner routing, exact Markdown mentions, and normalized member ownership are unchanged.
- **N1 remains addressed:** complete outbound payload SHA-256 still binds each intent, and changed payloads conflict before replay/send/reconcile.
- **N2 remains addressed:** gateway input and every public typed nested result are deep-copied.
- **N3 remains addressed:** missing/malformed decisions and malformed wait results are audited fail-closed without changing the effective decision.
- **N4 remains addressed:** gateway, client, and orchestrator share and consume one complete public record schema.
- **N5 remains addressed:** the strict default remains exactly `36000` seconds.

## Review Basis

- Reviewed manifest SHA-256: `47ed8206f0fe65d232e0e7f4e813085dbda1800e6016fe94e32f1e366799169c`
- Task brief SHA-256: `616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db`
- Round-4 re-review SHA-256: `0731713fea6738d17c63722f14bfcaa55f1aef9433248ea7160da7adccf3024d`
- Implementation report with round-5 appendix SHA-256: `33504c81927fe6b23f8cb52b0f2816d696990ec34c7e3c071a7466d0aedbeee5`
- Approval policy SHA-256: `ebf5950a883fdb1c61c69fcabf4a1cf42422be7b6efb6b40befff208817e0d13`
- External contracts SHA-256: `b7522ca349add10e3d2341f9c2be59593f9a08c85425b8b26a045939262cc9b6`

Current reviewed file hashes:

- `scripts/state_store.py` `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
- `scripts/requirement_snapshot.py` `fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9`
- `scripts/approval_contract.py` `d4b99f762a39b76fb9fc7f75b825df8da163a03767ae5cc4285fd5a5f0f52dbf`
- `scripts/collaboration.py` `148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825`
- `scripts/approval_ledger.py` `bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10`
- `scripts/orchestrator.py` `40a3c8e5523aa214eaff5faa4980a45f92e3a97dd9bb928e53a2698553627ce6`
- `scripts/evidence_gate.py` `9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321`
- `scripts/evidence_policy.py` `7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d`
- `scripts/clients/icafe_client.py` `886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1`
- `scripts/clients/infoflow_group_client.py` `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
- `scripts/clients/infoflow_approval_client.py` `175fe9aee20c796288a4944c990ca60465ccba66ef190d0d9c2a6ca94f685e7f`
- `scripts/tests/test_task5_safety.py` `996ff633871983fa83200451ad5248d3ae461d1b476deb41c527198bb7f328ca`
- `scripts/tests/test_collaboration.py` `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
- `scripts/tests/test_approval_channels.py` `5182449e44ba3329b329bc48ad5302f4162f2f064103687c32dfeeef66cf1ec3`
- `scripts/tests/test_gates.py` `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
- `scripts/tests/test_orchestrator.py` `ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99`
- `infoflow-gateway/gateway.py` `de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0`

The workspace is not a Git repository, so exact change-set provenance remains **UNVERIFIABLE**. The manifest and individual hashes identify the reviewed current scope. No runtime `tom-autorelease` or Codex reference appears in the reviewed Task 5 production files.

## Unverifiable Items

- The implementer's reported `80/80`, `217/217`, compilation, and scan results are **UNVERIFIABLE in this re-review** because execution was prohibited.
- Live Comate/Infoflow callback authentication, remote reconciliation behavior, credential configuration, deployment import paths, and installed group-script permissions remain **UNVERIFIABLE** under the fake-only/no-live-write constraint.
