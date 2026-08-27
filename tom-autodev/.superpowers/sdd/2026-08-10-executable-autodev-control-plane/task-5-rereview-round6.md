# Task 5 Exception Round 6 Re-review: Embedded Infoflow Collaboration and Approval

## Verdict

- Specification compliance: **PASS**
- Standards/code quality: **PASS**
- `N7`: **ADDRESSED**
- `C3`: **ADDRESSED**
- `I4`: **ADDRESSED**
- All other previously addressed findings remain addressed: `C1`, `C2`, `C4`, `C5`, `I1`, `I2`, `I3`, `N1`, `N2`, `N3`, `N4`, `N5`, and `N6`
- New Critical/Important findings: **none**
- Gate result: **PASS**

This was the authorized, scoped static exception re-review for `N7`, `C3`, and `I4`. No tests, compilation, live setup checks, network calls, implementation edits, or external writes were performed.

## Finding Classification

| Finding | Classification | Axis | Blocking | Disposition |
| --- | --- | --- | --- | --- |
| N7 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| C3 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| I4 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |

No scoped finding is `REJECTED_WITH_REASON` or `NEEDS_CLARIFICATION`.

## Specification Review

**PASS.** No blocking Specification finding remains.

### Critical N7: Reject error-bearing initial PENDING results

**ADDRESSED.** The shared `is_clean_initial_pending_result()` predicate requires all four initial-delivery invariants: `status == "PENDING"`, `reason_code is None`, `reply is None`, and `handoff is None` (`scripts/approval_contract.py:120-126`).

`InfoflowApprovalClient.request()` applies the predicate after complete parsing and before returning a result (`scripts/clients/infoflow_approval_client.py:19-33`). `Orchestrator._deliver_approval_channel()` independently applies the same predicate, along with the existing six-field request matcher, before either StateStore receipt persistence or `ApprovalLedger.record_delivery()` (`scripts/orchestrator.py:443-455`). A non-clean record is converted to the durable `APPROVAL_REQUEST_RESPONSE_INVALID` failure result rather than an Infoflow delivery receipt (`scripts/orchestrator.py:414-420`, `:446-452`).

The client regression test exercises every gateway-produced pending error code: `APPROVAL_ENVELOPE_INVALID`, `APPROVAL_INPUT_MISMATCH`, `APPROVAL_RESPONDER_UNAUTHORIZED`, and `APPROVAL_DECISION_INVALID` (`scripts/tests/test_approval_channels.py:177-225`). The raw-client orchestrator test exercises the same four values at the independent receipt boundary and asserts stable failure on an identical retry, exactly one send, zero reconcile calls, no Infoflow receipt, `PENDING`/unresolved approval state, and a Comate response blocked by `APPROVAL_DELIVERY_INCOMPLETE` (`scripts/tests/test_task5_safety.py:511-609`). Existing normalized clean-result and real gateway/client/orchestrator success coverage remains present (`scripts/tests/test_approval_channels.py:136-175`; `scripts/tests/test_task5_safety.py:611-680`).

### Critical C3: Dual-channel delivery and durable failure recovery

**ADDRESSED.** Error-bearing PENDING records can no longer satisfy Infoflow delivery. Client-raised and raw-orchestrator invalid response paths converge on the same durable failure marker. Because `_deliver_approval_channel()` reads that marker before completed-receipt lookup or reconciliation, an identical retry returns the stable error without a second send or reconcile (`scripts/orchestrator.py:414-425`, `:428-452`). No Infoflow receipt is recorded, so the ledger remains unresolved and Comate alone cannot authorize (`scripts/tests/test_task5_safety.py:593-609`).

The earlier six immutable-field binding remains enforced by `gateway_result_matches_request()` at both boundaries (`scripts/approval_contract.py:101-117`; `scripts/clients/infoflow_approval_client.py:26-32`; `scripts/orchestrator.py:443-452`). Intent conflict is still checked before failure replay, preserving changed-payload conflict behavior (`scripts/orchestrator.py:408-417`). Clean matching results still proceed to both StateStore and ledger receipt persistence (`scripts/orchestrator.py:453-455`).

### Important I4: Complete request-boundary regression coverage

**ADDRESSED.** The new direct client and raw-orchestrator tests cover all four public gateway error-bearing PENDING variants. At the orchestrator boundary the assertions cover the requested stable error, identical retry, one transport request, no reconciliation, no Infoflow delivery receipt, unresolved/PENDING state, and Comate delivery-incomplete rejection (`scripts/tests/test_task5_safety.py:519-609`). Positive clean pending coverage remains, including normalized policy/deadline acceptance and the real gateway/client/orchestrator pending-to-reply lifecycle (`scripts/tests/test_approval_channels.py:136-175`; `scripts/tests/test_task5_safety.py:611-680`).

## Standards Review

**PASS.** No blocking Standards finding remains and no new Critical/Important issue was identified.

The initial-request rule is centralized in the approval contract rather than duplicated. The client and orchestrator retain separate enforcement at their respective trust boundaries, which is appropriate defense in depth. The change reuses the existing deterministic delivery-failure mechanism and error vocabulary; it does not introduce a parallel state path or broaden the production interface.

The reported fixed-clock adjustment is confined to `scripts/tests/test_task5_safety.py:619-633`, where the explicit fixture deadline is 24 hours after its fixed clock. Production strict deadline behavior is unchanged: `STRICT_APPROVAL_TIMEOUT_SECONDS` remains exactly `36000`, and `ApprovalLedger.request()` still derives an omitted deadline from that constant (`scripts/approval_contract.py:9`; `scripts/approval_ledger.py:10`, `:39-47`).

## Previously Addressed Findings

- **C1 remains addressed:** snapshot intake and complete non-circular RequirementSnapshot hashing files are unchanged from the round-5 reviewed hashes.
- **C2 remains addressed:** strict channel/run/input/responder/member-policy checks and run-bound gates are unchanged; the scoped edits strengthen only initial Infoflow result acceptance.
- **C4 remains addressed:** wait, nested reply, malformed audit, timeout, and stable handoff production paths are unchanged; clean lifecycle coverage remains present.
- **C5 remains addressed:** group/message ownership, atomic write intent, and query-only recovery files are unchanged.
- **I1-I3 remain addressed:** owner routing, exact Markdown mentions, and normalized ownership production files are unchanged.
- **N1 remains addressed:** canonical outbound payload hashing and conflict-before-replay ordering remain intact (`scripts/orchestrator.py:408-417`).
- **N2 remains addressed:** gateway and public result deep-copy boundaries are unchanged.
- **N3 remains addressed:** malformed decision/wait-result audit paths are unchanged.
- **N4 remains addressed:** gateway, client, and orchestrator still share the complete public result schema.
- **N5 remains addressed:** the production strict default is `36000` seconds (`scripts/approval_contract.py:9`).
- **N6 remains addressed:** all six immutable request fields remain matched at both the client and orchestrator boundaries (`scripts/approval_contract.py:101-117`; `scripts/clients/infoflow_approval_client.py:26-32`; `scripts/orchestrator.py:443-452`).

All reviewed files unrelated to the scoped round-6 implementation/tests retain their round-5 SHA-256 values. The changes in `approval_contract.py`, `infoflow_approval_client.py`, and `orchestrator.py` are consistent with the N7 correction; the changes in the two test files provide N7 coverage plus the documented test-only clock adjustment.

## Review Basis

- Reviewed manifest SHA-256: `425615a880c5647922636436650ebbb9d05e93704c12ae6a31dd25d0e8bb553c` (SHA-256 of the ordered newline-delimited `path hash` records below)
- Task brief SHA-256: `616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db`
- Round-5 re-review SHA-256: `af1115f5548b87bad10e64d651f2a33c975bdc586962f799edcbcaa3306ec7be`
- Implementation report with round-6 appendix SHA-256: `247c42e419513746305bb9ad4bf71b0570e9d969a6ccfe1e9b97c96944526929`
- Approval policy SHA-256: `ebf5950a883fdb1c61c69fcabf4a1cf42422be7b6efb6b40befff208817e0d13`
- External contracts SHA-256: `b7522ca349add10e3d2341f9c2be59593f9a08c85425b8b26a045939262cc9b6`

Current reviewed file hashes:

- `scripts/state_store.py` `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
- `scripts/requirement_snapshot.py` `fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9`
- `scripts/approval_contract.py` `951a79e2296dd086b2e3c762546cbcfef9b850a9421290637fe983a71e58591a`
- `scripts/collaboration.py` `148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825`
- `scripts/approval_ledger.py` `bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10`
- `scripts/orchestrator.py` `616e29153a0abcfc5ef81335d1bbd638997c218062febf362b320c702c5e57c8`
- `scripts/evidence_gate.py` `9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321`
- `scripts/evidence_policy.py` `7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d`
- `scripts/clients/icafe_client.py` `886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1`
- `scripts/clients/infoflow_group_client.py` `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
- `scripts/clients/infoflow_approval_client.py` `76e4ea3e7bc37544f486b283490bc8125866fba1d08964d94c8fa3ae796ba76e`
- `scripts/tests/test_task5_safety.py` `f58eecdf24e3b3cd0604d4faf9474fc32d021dc482eefbf83126f3e93ce6b93e`
- `scripts/tests/test_collaboration.py` `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
- `scripts/tests/test_approval_channels.py` `70fb1659be53bcc9b3c8d08d605cbbeb94d8dfccac699b986b8d88abd8dcffbe`
- `scripts/tests/test_gates.py` `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
- `scripts/tests/test_orchestrator.py` `ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99`
- `infoflow-gateway/gateway.py` `de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0`

The workspace is not a Git repository, so exact change-set provenance remains **UNVERIFIABLE**. The manifest and individual hashes identify the exact current scope reviewed.

## Unverifiable Items

- The implementer's reported `6/6`, `82/82`, `219/219`, compilation, and dependency-scan results are **UNVERIFIABLE in this re-review** because execution was prohibited.
- Live Comate/Infoflow callback authentication, remote reconciliation behavior, credential configuration, deployment import paths, and installed group-script permissions remain **UNVERIFIABLE** under the static/fake-only review constraint.
