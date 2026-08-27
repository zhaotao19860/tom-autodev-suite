# Task 5 Round 3 Re-review: Embedded Infoflow Collaboration and Approval

## Verdict

- Specification compliance: **FAIL**
- Standards/code quality: **FAIL**
- Addressed in round 3: `C2`, `N2`, `N3`
- Still not addressed: `C1`, `C4`, `I4`
- Remain addressed: `C3`, `C5`, `I1`, `I2`, `I3`, `N1`
- New findings: Critical `N4`, Important `N5`
- Gate result: **FAIL**. Blocking confirmed findings remain, so Task 5 cannot pass this repair round.

This was a static, scoped re-review. No tests, compilation, live setup checks, network calls, or external writes were run.

## Finding Classification

| Finding | Classification | Axis | Blocking | Disposition |
| --- | --- | --- | --- | --- |
| C1 | `CONFIRMED` | Specification | Yes | Not addressed |
| C2 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| C4 | `CONFIRMED` | Specification and Standards | Yes | Not addressed |
| I4 | `CONFIRMED` | Specification and Standards | Yes | Not addressed |
| N2 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| N3 | `CONFIRMED` | Specification and Standards | No remaining issue | Addressed |
| N4 | `CONFIRMED` | Specification and Standards | Yes | New Critical |
| N5 | `CONFIRMED` | Specification | Yes | New Important |

No scoped finding is `REJECTED_WITH_REASON` or `NEEDS_CLARIFICATION`.

## Open-Finding Dispositions

### Critical C1: G0 must bind the canonical immutable RequirementSnapshot

**NOT ADDRESSED** (Specification, blocking).

The no-snapshot half of the correction is present. `Orchestrator.start()` omits `collaboration_binding` when no snapshot is supplied, and the ledger-backed `CollaborationSession.prepare_g0()` then returns `G0_BINDING_MISMATCH` because `_run_context()` cannot obtain the required binding (`scripts/orchestrator.py:42-65`; `scripts/collaboration.py:56-65`, `:168-192`). A strict G0 group therefore cannot be prepared from caller title/member data alone.

The snapshot accepted by the production start path is not established as a canonical RequirementSnapshot, however. `_collaboration_binding()` reads only `canonical_card_id`, `title`, and `content_hash`, accepts any matching ID/nonempty string/64-hex value, and stores those three values without recomputing the hash over the supplied snapshot (`scripts/orchestrator.py:458-485`). The canonical iCafe adapter constructs `content_hash` by hashing the complete snapshot containing title, body/HTML, acceptance/fields, attachments/links, status/type, responsible people, and created/modified metadata (`scripts/clients/icafe_client.py:100-135`, `:703-705`). The new start test instead supplies only three fields and an arbitrary `"a" * 64` value (`scripts/tests/test_task5_safety.py:77-110`), so it proves shape acceptance, not canonical snapshot/hash binding.

Concrete impact: a caller can bind an arbitrary title and unrelated 64-hex value to the run, and that title becomes the G0 group name while the unrelated value is included in the G0 input hash. The control plane does not prove that the title and content identity came from the immutable iCafe snapshot required by G0.

Required correction: accept the complete RequirementSnapshot shape and verify its canonical `content_hash` using the same canonicalization as the iCafe adapter before persisting the binding, or accept a trusted persisted snapshot descriptor whose provenance/hash has already been verified. Add a negative production-path test for a title/content/hash mismatch.

### Critical C2: Exact channel, run, responder, gate, and full-email policy

**ADDRESSED** (Specification and Standards, no remaining blocking issue from C2).

Strict ledger rows remain run-bound, require a matching run and responder, require delivery receipts for exactly Comate and Infoflow, and run-bound gates reject legacy or cross-run approval evidence (`scripts/approval_ledger.py:39-89`, `:178-190`; `scripts/evidence_policy.py:53-65`; `scripts/orchestrator.py:344-353`, `:502-523`).

The round-three gateway correction now requires `channel == "infoflow"`, exactly the `comate` and `infoflow` policy keys, a nonempty list for each, and full-email-shaped members before storing a request (`infoflow-gateway/gateway.py:113-131`). Reply validation binds run, approval, exact channel, input hash, and the stored channel membership (`infoflow-gateway/gateway.py:61-80`). The focused unsupported-channel and malformed-email cases exercise these boundaries (`scripts/tests/test_task5_safety.py:285-318`).

### Critical C4: One executable deadline/timeout/heartbeat/handoff lifecycle

**NOT ADDRESSED** (Specification and Standards, blocking).

The deadline storage and timeout transition pieces are materially improved. A strict ledger request now persists an explicit deadline, the orchestrator forwards that stored value, the gateway requires and normalizes it, retry identity includes it, and gateway `wait()`, `reply()`, and explicit `timeout()` all route expiry through `_transition_timeout()` (`scripts/approval_ledger.py:39-56`; `scripts/orchestrator.py:373-385`; `infoflow-gateway/gateway.py:21-59`, `:61-92`, `:101-110`). Gateway and ledger use the same stable handoff ID/payload, and `StateStore.record_handoff()` is idempotent for that identity (`scripts/state_store.py:362-387`).

The real lifecycle is still not executable end to end because the gateway/client return shape and orchestrator consumption shape disagree. `PendingApprovalGateway.wait()` returns the complete stored record. A pending record contains many fields, and an accepted response is represented as `status="APPROVE"` with `reply={"decision": ..., "responder": ...}` nested inside (`infoflow-gateway/gateway.py:31-43`, `:52-59`, `:77-80`). `InfoflowApprovalClient.wait()` forwards that dictionary unchanged (`scripts/clients/infoflow_approval_client.py:21-27`, `:30-36`). `Orchestrator.wait_infoflow_approval()` recognizes only `TIMEOUT` or a *minimal* two-field pending dictionary; every other record is sent to `receive_infoflow_reply()`, which requires top-level `decision` and `responder` (`scripts/orchestrator.py:420-446`). Consequently, a normal full pending gateway record is audited as a malformed envelope, and a valid accepted gateway reply is also audited as missing `decision`; neither can produce the first effective ledger decision.

The default deadline is also inconsistent with the controlling approval policy; see `N5`.

Concrete impact: the shipped gateway/client/orchestrator chain cannot accept a valid Infoflow approval and can create invalid-response audit rows for ordinary pending polls. Timeout happens to work because `status="TIMEOUT"` is handled specially, but this is not a closed approval state machine.

Required correction: define one typed public gateway result schema. Either return the accepted reply envelope at the top level, or make the orchestrator explicitly interpret gateway records and their nested `reply`; accept a full validated pending record without auditing it. Exercise the actual gateway through `InfoflowApprovalClient` and `Orchestrator.wait_infoflow_approval()`.

### Important I4: Boundary and state-machine regression coverage

**NOT ADDRESSED** (Specification and Standards, blocking).

Round three adds useful cases for no-snapshot G0 blocking, unsupported channels/full-email rejection, input/output member-policy mutation, shared explicit deadlines, all gateway timeout entry points, malformed decisions, and changed retry deadlines (`scripts/tests/test_task5_safety.py:77-110`, `:285-366`).

The tests still avoid the failing production boundaries. The canonical start test neither uses a complete RequirementSnapshot nor checks a mismatched canonical hash. Orchestrator wait tests use custom transports that return a top-level reply envelope instead of connecting `PendingApprovalGateway` through `InfoflowApprovalClient` (`scripts/tests/test_task5_safety.py:347-414`). The client pending fake returns only `{request_id, status}`, exactly the special shape accepted by the orchestrator rather than the full gateway record (`scripts/tests/test_approval_channels.py:17-29`, `:100-110`). No test asserts the required ten-hour default.

Concrete impact: the reported suite can remain green while `C1`, `C4`, `N4`, and `N5` remain reachable.

## New-Finding Dispositions

### Critical N4: The real gateway result schema is incompatible with the orchestrator wait schema

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: `PendingApprovalGateway.reply()` stores the canonical response under nested `reply` and returns a record (`infoflow-gateway/gateway.py:77-80`); `wait()` returns that record (`:52-59`); the client preserves it (`scripts/clients/infoflow_approval_client.py:21-36`); the orchestrator expects top-level decision/responder (`scripts/orchestrator.py:420-446`).
- Affected criteria: approval contracts 1-5; first-valid response; executable request/reply/wait lifecycle; invalid-response audit accuracy.
- Concrete impact: a legitimate Infoflow approval cannot become effective through the production components, and an ordinary pending poll becomes a false malformed-envelope audit.
- Required correction: publish and consume one common typed record/reply schema and add an end-to-end fake gateway/client/orchestrator regression.

### Important N5: The strict default deadline is 30 minutes instead of the required ten hours

- Classification: `CONFIRMED`
- Axis: Specification
- Blocking: yes
- Evidence: `STRICT_APPROVAL_TIMEOUT_SECONDS` is `1800`, and strict requests without an explicit deadline use that constant (`scripts/approval_ledger.py:12-15`, `:39-48`). The controlling approval policy says, "Default wait limit is ten hours" (`references/approval-policy.md:16`). The round-three tests verify only that a deadline exists/is shared, not its default duration (`scripts/tests/test_task5_safety.py:394-454`).
- Affected criteria: approval timeout policy and stable stop/handoff behavior.
- Concrete impact: normal approval requests can time out after 30 minutes, 9.5 hours earlier than the approved control-plane policy.
- Required correction: use the policy's ten-hour default (`36000` seconds) from one shared constant and assert the resulting ledger/gateway deadline.

## Addressed-Finding Confirmation

- **N2 remains addressed:** the gateway deep-copies the nested policy on request input and deep-copies every stored-record return, including request replay, wait, reply, timeout, heartbeat, and error overlays (`infoflow-gateway/gateway.py:21-50`, `:52-99`). Mutating the original request or returned records cannot add an authorized responder.
- **N3 is addressed:** `receive_infoflow_reply()` routes a missing/non-string decision to `reject_envelope()` and routes malformed decision strings through `ApprovalLedger.receive()`, where `_reject()` records `APPROVAL_DECISION_INVALID` without changing the effective decision (`scripts/orchestrator.py:435-446`; `scripts/approval_ledger.py:63-89`, `:127-149`, `:178-192`). The focused test checks both audit rows (`scripts/tests/test_task5_safety.py:347-366`). `N4` is separate: it concerns a valid gateway record being represented in a shape that this corrected malformed-envelope path cannot consume.
- **C3 remains addressed:** Comate and Infoflow deliveries share an approval ID/input/payload, claim per-channel durable intents before external calls, replay completed receipts locally, and reconcile pending outcomes without automatic resend (`scripts/orchestrator.py:359-418`; `scripts/approval_ledger.py:109-118`, `:186-190`).
- **C5 remains addressed:** group and message writes use run-scoped atomic `claim_intent()` ownership and query/reconcile nonowner or unknown results (`scripts/collaboration.py:86-120`, `:136-166`).
- **I1 remains addressed:** auth/platform/release-rule failures target only the canonical stored owner (`scripts/collaboration.py:122-134`).
- **I2 remains addressed:** rendered Markdown mentions must exactly equal `at_users`, and untrusted fields neutralize `@` (`scripts/collaboration.py:136-166`, `:250-259`).
- **I3 remains addressed:** normalized fixed project members provide the canonical owner; dictionary member entries are normalized before use (`scripts/collaboration.py:29-54`).
- **N1 remains addressed:** each approval delivery intent binds the SHA-256 of the complete canonical outbound envelope, so changed evidence/member/deadline content conflicts before reconciliation or send (`scripts/orchestrator.py:390-418`).

## Review Basis

- Reviewed manifest SHA-256: `838f58ccaeb48a970dc3da3dfb4e38f06f2ad02e2a9ba97ec9d0f5a86ad80b05`
- Task brief SHA-256: `616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db`
- Round-2 re-review SHA-256: `f3ea401294089e4cbb0ebb52e480438f49619264539e2fe59b7aa18afd8035c8`
- Implementation report with round-3 appendix SHA-256: `29abd34ac955f0f30299daf7e46dd090982c02e5ee5b322cd8f33720d886151b`
- Approval policy SHA-256: `ebf5950a883fdb1c61c69fcabf4a1cf42422be7b6efb6b40befff208817e0d13`
- External contracts SHA-256: `b7522ca349add10e3d2341f9c2be59593f9a08c85425b8b26a045939262cc9b6`

Current reviewed file hashes:

- `scripts/state_store.py` `79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c`
- `scripts/collaboration.py` `148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825`
- `scripts/approval_ledger.py` `d82f60a877a6d83dd6c5b502c29b13497da7f347dd1f8d6cbaba26db606c8653`
- `scripts/orchestrator.py` `572fb01503ea0911f7eb20039d5deb5e0cfe5c9a50cd0385cc5baf7822ec0dba`
- `scripts/evidence_gate.py` `9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321`
- `scripts/evidence_policy.py` `7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d`
- `scripts/clients/infoflow_group_client.py` `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
- `scripts/clients/infoflow_approval_client.py` `50f1351bf223e0a01b04c8518e3e4d219fee23b145bb02670d9b89bab306c76f`
- `scripts/tests/test_task5_safety.py` `1fcbf2fa93cf91ae14daadcc33b551091ca102e099fa46b1a84ec360a3e0ea10`
- `scripts/tests/test_collaboration.py` `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
- `scripts/tests/test_approval_channels.py` `69f3c88505aecd4bf53707767aaf1003efbdb20a21b580f8558c07cc41365f2a`
- `scripts/tests/test_gates.py` `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
- `scripts/tests/test_orchestrator.py` `ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99`
- `infoflow-gateway/gateway.py` `339633e3824fcd05e58af8c65a7487f7863d318a0e1f7607a383671179db1fe9`

The workspace is not a Git repository, so exact change-set provenance remains **UNVERIFIABLE**. The manifest and individual hashes identify the complete reviewed current scope. No runtime `tom-autorelease` or Codex reference appears in the reviewed Task 5 production files.

## Unverifiable Items

- The implementer's reported `16/16`, `71/71`, `208` tests, compilation, and scan results are **UNVERIFIABLE in this re-review** because execution was prohibited.
- Live Comate/Infoflow callback authentication, remote reconciliation behavior, credential configuration, and installed group-script permissions remain **UNVERIFIABLE** under the fake-only/no-live-write constraint.
- Human confirmation/provenance of a supplied RequirementSnapshot is not represented by the reviewed start interface; only its static dictionary handling could be assessed.
