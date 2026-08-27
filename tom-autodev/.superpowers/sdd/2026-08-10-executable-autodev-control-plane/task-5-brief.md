# Task 5: Embedded Infoflow Collaboration and Approval

## Files

- Create `scripts/collaboration.py`.
- Create `scripts/clients/infoflow_group_client.py` and `scripts/clients/infoflow_approval_client.py`.
- Create `infoflow-gateway/` by extracting only the required gateway implementation and tests from `/Users/tom/Desktop/skills/tom-autorelease`; production code must not import, execute, or resolve that Skill at runtime.
- Modify `scripts/approval_ledger.py` and `scripts/orchestrator.py` only for collaboration and approval wiring required by this task.
- Add `scripts/tests/test_collaboration.py` and update `scripts/tests/test_approval_channels.py` or the existing approval-channel test module.

## Interfaces

- `CollaborationSession.create(run_id, project, card, members) -> dict`
- `CollaborationSession.route_failure(failure_bundle) -> dict`
- `InfoflowGroupClient.create_or_reuse(group_request) -> dict`
- `InfoflowGroupClient.send_markdown(group_id, content, at_users, idempotency_key) -> dict`
- `InfoflowApprovalClient.request(request) -> dict`
- `InfoflowApprovalClient.wait(request_id, timeout_seconds) -> dict`

## Collaboration contract

1. Create or reuse at most one collaboration group per run, only after G0 approval binds the canonical group name, owner and member snapshot. Group creation is itself a G0 side effect and must use durable intent/receipt persistence.
2. Resolve members in this order: fixed full-email dev/test/owner members from the validated project profile; configured iCafe responsible-person fields; approved card owners only when the profile explicitly allows them. Never guess or synthesize an email. Any unresolved or non-full email returns `MEMBER_CONFIRMATION_REQUIRED` before external calls.
3. The group name is `<project>-<icafe-card>-<short-title>-研发测试协作`. Use the extracted group script behavior with `friendlyLevel=3`; run the embedded setup check before a live create. Tests must inject fake transports and must not create a real group.
4. Persist `group_id`, group name/owner, role member lists, immutable member snapshot, bot ID, message receipts, approval request IDs and heartbeat evidence. Do not persist tokens or raw secret-bearing configuration.
5. Exact role routing is: environment/test-data/test-case/regression/integration assertion failures -> test; code/interface/Spec or Task Plan deviation/Review finding -> dev; unclear or mixed code/test boundary -> both; auth/platform/release-rule failure -> project owner.
6. Group messages use Markdown. The rendered content and `atUsers` must identify the same target full emails. Include available canonical iCafe/KU/revision/pipeline/build/stage/job evidence, a bounded failure summary and next action. Missing evidence stays absent rather than guessed.
7. Message delivery is idempotent. Persist intent before send, query/reconcile an unknown result, and never resend an unknown write automatically. Repeated calls with the same canonical input return the prior receipt; changed content under the same idempotency key is a conflict.

## Approval contract

1. Extract the old gateway's request/reply/wait/heartbeat/timeout behavior into this Skill. Keep credentials in the existing external config and expose no runtime path reference to `tom-autorelease`.
2. Comate and Infoflow remain the only supported approval channels. They share one approval ID and canonical input hash. The first valid response wins; conflicting, late and invalid responses are audit records only and cannot change the effective decision.
3. Persist delivery receipts, channel, responder identity, membership/role validation, canonical decision, received time, timeout, conflict and late-response metadata without raw secret data.
4. Input hash change invalidates the request. Timeout returns a stable stop/handoff result. A heartbeat may extend liveness evidence but must not silently extend the approved input, deadline or decision.
5. Validate the responder against the approved channel/member policy. An unrequested channel, unknown member, malformed decision or mismatched approval/run/input is rejected and audited fail-closed.

## Extraction and safety boundaries

- Read `/Users/tom/Desktop/skills/tom-autorelease` only as source material. Copy and adapt the smallest necessary Infoflow implementation and tests; do not copy iCode/iPipe/release code in this task.
- The new runtime must be self-contained under `tom-autodev` and use injected transports at the Python boundary.
- Do not add Codex support. Do not change business workspaces, project profiles, group membership, iCafe/KU content or pipeline state.
- Do not run BGW/XFlow compile, unit, regression, integration, Docker, NCS, simulator or release commands on Mac.

## Required TDD and verification

1. First add failing tests for fixed/profile/iCafe member resolution, unresolved full email, G0 binding, duplicate group creation, unknown-result no-replay, duplicate/conflicting messages, all four responsibility routes, approval first-valid-wins, timeout, late/conflicting response and channel/member rejection.
2. Record the focused RED command and expected failures in the report before production edits.
3. Implement the minimal collaboration/group/approval behavior, then run focused tests, full `scripts/tests` discovery and `py_compile`/`compileall`.
4. Use fake transports only. No live Infoflow, iCafe, KU, iCode or iPipe write is allowed in tests.
