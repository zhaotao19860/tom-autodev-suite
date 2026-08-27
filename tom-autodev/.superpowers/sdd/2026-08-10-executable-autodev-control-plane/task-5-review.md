# Task 5 Review: Embedded Infoflow Collaboration and Approval

## Confirmed Findings

### Critical C1: G0 collaboration authorization is caller-forgeable and is not bound to the canonical run/profile/card

- Classification: `CONFIRMED`
- Axis: Specification
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:73` reads a `g0_approval` dictionary from the caller-supplied `members` object. `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:220` only compares fields in that dictionary; it never looks up the approval in `ApprovalLedger`, checks an approval ID/input hash, or establishes that the run, project, card, profile hash, group name, owner, and member snapshot were approved together. `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:337` merely returns a `CollaborationSession`; it does not load the run's validated project profile or supply a ledger-backed G0 record. The test itself constructs the purported approval locally at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py:54`.
- Concrete impact: any caller can fabricate an `APPROVE` dictionary and create an Infoflow group for an arbitrary or nonexistent run, arbitrary project/card, and arbitrary member set. G0 is therefore not an authorization boundary for the external side effect.
- Affected criteria: collaboration contract 1 and 2; G0-bound canonical name/owner/member snapshot; fail-closed external writes.
- Required correction: accept a durable G0 approval identifier and canonical input hash, resolve it from the ledger, bind it to the existing run's recorded project/profile/card snapshot, and persist that approval binding in the group-create intent.

### Critical C2: Approval identities are global rather than run-bound, and responder validation fails open

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: the approval table has no `run_id` at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:24`, and the unique identity is only `(action, input_hash)` at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:39`. `ApprovalLedger.request()` consequently reuses that identity at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:83` across every run. `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:253` retrieves any supplied approval ID without checking ownership by the current run. Separately, `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:308` turns an omitted member policy into `{}`, and `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:332` rejects a responder only when a nonempty per-channel list happens to exist. `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:328` and the CLI invocation at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:401` do not accept or forward responder identity at all. The embedded gateway also records no run identity at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:23` and validates no channel/member/approval/run tuple in `reply()` at `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:52`.
- Concrete impact: an approval from one run can authorize another run with the same gate and input hash. With the default policy, an anonymous response is accepted; with a configured policy, the shipped orchestrator/CLI path cannot supply the identity needed to pass it. Mismatched run and approval claims cannot be audited or rejected because they are not represented.
- Affected criteria: approval contract 2, 3, and 5; first valid authorized responder wins; mismatched approval/run/input rejection.
- Required correction: make `run_id` part of the durable approval identity and every request/response, require nonempty policies for both exact channels, require authenticated responder identity, and validate the complete run/approval/channel/member/input tuple before recording an effective response.

### Critical C3: Approval delivery is neither dual-channel nor protected by durable write intent/reconciliation

- Classification: `CONFIRMED`
- Axis: Specification
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:350` creates a ledger row naming both channels, but `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:357` permits returning without delivering anything and `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:359` sends only to the Infoflow client. There is no Comate delivery call or receipt. The external call occurs before the only durable receipt write at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:364`; there is no `StateStore.intent`, idempotency key, unknown-result reconciliation, or query-only result. Repeating the method for the ledger-deduplicated request calls the external client again. `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:186` also includes a new timestamp inside every delivery entry, so repeated identical receipts cannot deduplicate. The request sent at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:359` omits the required evidence summary.
- Concrete impact: an exception or process crash after remote acceptance but before `record_delivery()` leaves no durable intent and a retry can publish a duplicate approval. A nominal approval can exist with zero or one delivered channel while appearing configured for both, silently reducing the required Comate+Infoflow control. The two channels are not proven to receive identical content.
- Affected criteria: approval contract 1 through 3; extraction/safety fail-closed write boundary; `/Users/tom/Desktop/skills/tom-autodev/references/external-contracts.md:23` dual-channel publication rule.
- Required correction: model Comate and Infoflow deliveries as separate durable intents under one approval ID/canonical payload hash, persist intent before each external call, reconcile unknown outcomes without replay, record explicit delivery failure, and do not activate waiting/decision acceptance until the exact payload is accounted for on both required channels.

### Critical C4: Deadline, timeout, late-response, heartbeat, and handoff behavior is not a closed state machine

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:79` accepts any deadline string without parsing. `receive()` at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:123` never compares receipt time with the deadline, so the first response after expiry becomes effective until some caller separately invokes `timeout()`. Conversely, `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:208` permits timeout before the deadline. If a decision is already effective, the same method still returns `APPROVAL_TIMEOUT` and a pending handoff at `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:228`. That handoff is only a returned dictionary; it is not persisted through `StateStore.record_handoff` and cannot appear in recovery. The gateway has the same ordering dependency: `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:47` marks expiry only during `wait()`, while `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py:52` accepts a reply without checking the deadline. `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:341` exposes request delivery only and wires no wait, reply, heartbeat, timeout, or handoff flow.
- Concrete impact: a genuinely late approval may win; an unexpired approval may be forcibly timed out; an already approved request may be reported as timed out; and the claimed timeout handoff is absent from durable recovery state. Heartbeat/deadline behavior exists only in isolated helpers, not in an executable orchestration path.
- Affected criteria: approval contract 2 through 5; stable timeout stop/handoff; late responses audit-only; heartbeat cannot alter deadline/input/decision.
- Required correction: parse and bind a single immutable deadline, atomically evaluate expiry before every reply, allow timeout only at/after that deadline while pending, persist one run-bound handoff, and wire gateway wait/reply/heartbeat/timeout events into the durable ledger/orchestrator path.

### Critical C5: Group and message idempotency can issue duplicate external writes

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: the group idempotency key at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:83` hashes the whole request, so a changed approved-looking payload for the same run gets a different key and can create a second group; there is no run-level uniqueness check. The check-then-insert sequence at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:84` and `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:95` is also unsafe under concurrent callers: both can observe no intent, `StateStore.intent()` can return the same durable intent to the loser, and both then execute `create_or_reuse()` at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:97`. The same race exists for messages between `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:134` and `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:147`.
- Concrete impact: one run can own multiple external groups, and concurrent retries can create duplicate groups or send duplicate messages even for identical canonical input. The external scripts expose no reconciliation API, so those duplicates cannot be repaired automatically afterward.
- Affected criteria: collaboration contract 1 and 7; at most one group per run; intent-before-send; repeated canonical calls return the prior receipt.
- Required correction: enforce a durable unique run/group operation identity independent of payload, atomically claim send ownership, return `QUERY_REQUIRED` to nonowners observing a pending intent, and apply the same claim protocol to message sends.

### Important I1: Project-owner routing also mentions optional card owners and other project-role members

- Classification: `CONFIRMED`
- Axis: Specification
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:49` appends allowed card owners to the `project` role. Auth/platform/release-rule routing selects that role at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:25`, and `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:112` mentions every member in it. The canonical owner used for group creation is separately chosen at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:72`.
- Concrete impact: owner-only operational/security failures are disclosed and assigned to optional card owners (and any iCafe project-role additions), contrary to the exact project-owner route.
- Affected criteria: collaboration contract 2 and 5; exact responsibility routing.
- Required correction: retain the canonical project owner as a distinct identity and use only it for owner-only routes; keep additional approved members in the group snapshot without conflating them with that route.

### Important I2: Markdown and `atUsers` are only checked in one direction

- Classification: `CONFIRMED`
- Axis: Specification and Standards
- Blocking: yes
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:130` verifies that every `at_users` entry appears in the content, but it does not reject additional full-email mentions in the Markdown. `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_group_client.py:50` repeats the same one-way check. Failure summary, evidence, and next action are caller-derived Markdown at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:247` and can introduce additional mentions.
- Concrete impact: the visible Markdown can identify or notify people who are absent from `atUsers`, so rendered responsibility and platform targeting can diverge. This defeats the exact routing and audit contract.
- Affected criteria: collaboration contract 5 and 6.
- Required correction: parse canonical full-email mentions from the rendered Markdown (or render from structured, escaped fields) and require exact set equality with `atUsers`.

### Important I3: Supported member-object input does not produce a canonical group owner

- Classification: `CONFIRMED`
- Axis: Standards and Specification
- Blocking: no
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:202` explicitly accepts either email strings or dictionaries containing `email` when resolving members. Group creation then ignores the normalized role list and reads `members["project"][0]` directly at `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py:72`. For the supported dictionary form, `owner` is a dictionary rather than the resolved full-email string and cannot satisfy the intended binding/client validation. If fixed project members are absent but iCafe resolution filled that role, this access can raise `KeyError` after resolution reported `OK`.
- Concrete impact: a shape accepted by member resolution cannot complete group creation reliably and may return a misleading G0 error or raise instead of the required structured `MEMBER_CONFIRMATION_REQUIRED` result.
- Affected criteria: collaboration interface and contract 2; stable fail-closed member resolution.
- Required correction: select the owner from the normalized fixed project-owner list and make missing/ambiguous owner a structured pre-write validation failure.

### Important I4: Required tests omit the production boundaries and the failing state-machine cases

- Classification: `CONFIRMED`
- Axis: Standards and Specification
- Blocking: yes
- Evidence: collaboration tests inject `FakeGroupClient` at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py:13`, so `InfoflowGroupClient` itself, `setup.sh --check`, argument shape, response normalization, and secret/error behavior are never exercised with a fake transport. Sequential duplicate tests at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py:114` and `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py:193` do not cover changed per-run group payloads, concurrent intent claims, unknown message sends, or extra Markdown mentions. Approval tests call ledger/client/gateway pieces independently at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py:30`, but there is no test of `Orchestrator.request_infoflow_approval`, dual-channel delivery, approval delivery unknown-result recovery, run binding, anonymous responder rejection, early timeout, reply-after-deadline-before-wait, durable handoff recovery, or post-decision timeout. The sole timeout test calls timeout only after an already-past deadline at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py:83`, hiding the missing deadline enforcement.
- Concrete impact: the reported green suite can pass while every Critical control-plane failure above remains reachable. It does not satisfy the task's required TDD coverage for the real adapter/wiring and fail-closed paths.
- Affected criteria: required TDD and verification items 1 through 4.
- Required correction: add fake-transport tests at the concrete client and orchestrator boundaries, including concurrency and restart cases, before accepting a repair.

## Dual-Axis Verdict

- Specification compliance: **FAIL**
- Code quality/standards: **FAIL**
- Confirmed findings: 5 Critical, 4 Important, 0 Minor
- Gate result: **FAIL**. Blocking `CONFIRMED` findings remain; Task 5 must return to diagnosis/repair and cannot proceed as a passing review.

## Review Basis

- Review mode: static, complete-current-file review requested because `/Users/tom/Desktop/skills/tom-autodev` is not a Git repository. `git rev-parse --show-toplevel` reports no worktree, so an exact revision/diff baseline does not exist.
- Baseline manifest SHA-256: `d2071bbb2abc952d1d021e2f3d7482dea807bcb9313d48e9b3612a66e86faa3d` over the ten requested implementation/test/documentation files in the task-specified order.
- Task brief SHA-256: `616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db`.
- Implementer report SHA-256: `678af08f1e343d4fb525ab1d072629cfeed9e7cbce04dc312d13191c4e75e2b9`.
- Reviewed file hashes:
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/collaboration.py`: `730dc0a828a8c75ed3bc2f6e6a82b3a034a45b9ea48cc3073ea1ebba5dd9486e`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_group_client.py`: `cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/infoflow_approval_client.py`: `75beceb6ec05e22cb4f56973ebc80c95c7bd2c640266936c7349cf9b6faf2ba2`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py`: `3f2c07752ee6bee83c0cfbff6807c5cc3795fe3083a58fae556153da6783cfcf`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py`: `dbc101557689ff1701f1a7b68ca238bee692c8fc83dad93e2ccb7c50a00d14bd`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py`: `6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py`: `aad6811380f247882d7712d47b4d18822e79b814b07c20de9813172d7095e577`
  - `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_gates.py`: `79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719`
  - `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/gateway.py`: `fbc2eb9110a1c21c4ec48de736cd49999567b31e8f1749a95925fc7971e0c2b1`
  - `/Users/tom/Desktop/skills/tom-autodev/infoflow-gateway/README.md`: `235f3ef9ee37e2018984d095aed79d3df292f6f43c0d8879a1f9dc60dd17fdee`
- No tests, compilation, live setup checks, network calls, BGW/XFlow commands, or external writes were run during this review, as instructed.

## Passing Static Checks

- No runtime reference to `tom-autorelease` or Codex support appears in the reviewed Task 5 production files.
- The group client uses the Comate-installed group-script path and invokes `setup.sh --check` before `group_create.sh`; `friendlyLevel=3` is enforced.
- Group and message happy paths do persist a `StateStore` intent before their external calls, and pending unknown results do not automatically replay in sequential calls.
- ApprovalLedger uses a SQLite immediate transaction for response resolution, so sequential/concurrent ledger responses with a valid stored identity preserve the first effective decision.
- Secret-key rejection exists at the durable `StateStore` and approval delivery receipt boundaries; no obvious raw credential configuration is intentionally persisted by the reviewed happy paths.
- Task 5 test transports are fake/local only; no live service call is present in the reviewed tests.

## Unverifiable Items

- Exact change-set provenance is **UNVERIFIABLE** because there is no Git baseline or diff. This review hashes and assesses the complete current files named by the task instead.
- The implementer report's RED/GREEN/full-discovery/compile results are **UNVERIFIABLE in this review** because the reviewer was explicitly instructed not to run tests. They are treated as implementer-provided evidence, not independently reproduced results.
- Live Comate/Infoflow credentials, setup permissions, script response shapes, callback authenticity, and remote reconciliation capabilities are **UNVERIFIABLE** under the fake-only/no-live-write constraint.
- The claim that the gateway was extracted from the smallest necessary `tom-autorelease` source is **UNVERIFIABLE from the requested baseline**. Static inspection does verify that the resulting reviewed runtime files contain no reference to that source Skill.

No external provider suggestions were supplied. Every issue listed in Confirmed Findings is classified exactly once as `CONFIRMED`; no `NEEDS_CLARIFICATION` finding is required to reach the failing verdict.
