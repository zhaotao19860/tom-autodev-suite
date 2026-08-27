# Task 5 Report

## RED Evidence

Before production edits, ran:

```bash
python3 -m unittest /Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_collaboration.py /Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_approval_channels.py -v
```

Expected result: failure because the requested collaboration and approval implementation did not exist. Observed two import failures:

- `ModuleNotFoundError: No module named 'collaboration'`
- `ModuleNotFoundError: No module named 'clients.infoflow_approval_client'`

The tests cover member resolution, G0 binding, group/message idempotency, unknown write recovery, all role routes, approval first-valid-wins, invalid channel/member/decision rejection auditing, timeout and late response behavior, plus the extracted gateway heartbeat/deadline boundary.

## Changed Files

- `scripts/collaboration.py`: G0-bound group lifecycle, member resolution, durable create/message intents, unknown-write reconciliation and bounded Markdown failure routing.
- `scripts/clients/infoflow_group_client.py`: injected boundary around the Comate-installed group scripts; it performs `setup.sh --check` before a create and requires `friendlyLevel=3`.
- `scripts/clients/infoflow_approval_client.py`: injected request/wait boundary that removes secret-bearing response fields before returning data.
- `infoflow-gateway/gateway.py` and `infoflow-gateway/README.md`: self-contained extraction of pending request, reply, wait, heartbeat and immutable deadline behavior.
- `scripts/approval_ledger.py`: durable member policies, delivery receipts, heartbeats, invalid/late/conflict response audit records, first-valid decision semantics and timeout handoff result.
- `scripts/orchestrator.py`: explicit collaboration-session and Infoflow approval-request wiring.
- `scripts/tests/test_collaboration.py`, `scripts/tests/test_approval_channels.py`, and `scripts/tests/test_gates.py`: collaboration and approval contract coverage, including audited malformed decisions.

## Verification

Focused GREEN:

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py -v
```

Result: `Ran 12 tests ... OK`.

Ledger compatibility and audit behavior:

```bash
python3 -m unittest scripts/tests/test_gates.py -v
```

Result: `Ran 19 tests ... OK`.

Full control-plane discovery:

```bash
python3 -m unittest discover -s scripts/tests -q
```

Result: `Ran 191 tests in 11.389s` and `OK`.

Static validation:

```bash
python3 -m py_compile scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n '/Users/tom/Desktop/skills/tom-autorelease|tom-autorelease' scripts infoflow-gateway
```

Result: both compilation commands passed; the runtime-reference scan produced no matches.

## Extraction Provenance

Read-only source material: `/Users/tom/Desktop/skills/tom-autorelease/infoflow-gateway/src/server.mjs` and its package/readme. The extraction keeps only `PendingStore`-equivalent request creation, reply resolution, bounded wait, heartbeat evidence and timeout behavior. It intentionally excludes the old Node server, SDK initialization, transport credentials, iCode, iPipe and release code. The new runtime has no source or runtime path reference to `tom-autorelease`.

The group boundary follows the installed group-script contract and defaults to `/Users/tom/.comate/skills/infoflow-message-group/scripts`; tests use `FakeGroupClient` and `FakeApprovalTransport` only. No Infoflow, iCafe, KU, iCode or iPipe write was attempted.

## Self-Review

- Group create and message send persist durable intents before the external call. Completed receipts replay; pending unknown results query/reconcile and never auto-resend the write.
- Member validation requires full email addresses before an external call. Canonical group owner remains the profile project owner; optional card owners only augment the member snapshot.
- Markdown recipients and `at_users` carry the same full emails. Failure content includes only available evidence and bounds the summary to 800 characters.
- Approval requests require exactly Comate and Infoflow. The ledger accepts the first valid decision, audits conflict/late/invalid replies, validates configured members and preserves timeout as a stable handoff. `record_heartbeat` does not modify input hash, deadline or decision.
- State persistence rejects secret-bearing keys. The new clients do not load or persist credentials; live credentials remain in external configuration.

Remaining acceptance boundary: no live Comate Infoflow setup check or group creation was run, by task safety constraints. A separately authorized live preflight should validate deployment credentials and group-script API permissions.

## Fix Round 1 RED Evidence

Before fix-round production edits, ran:

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -v
```

Observed the expected five API failures: `ApprovalLedger.request()` lacked `run_id`; `CollaborationSession` lacked the ledger-backed `approvals` dependency and G0 preparation/binding API. The concrete `InfoflowGroupClient` fake-transport command-shape test already passed. This establishes the safety test as a regression detector rather than post-hoc coverage.

## Fix Round 1 GREEN Evidence

The safety suite now covers run/profile/card/profile-hash G0 binding, authenticated member policy and run ownership, atomic one-group/one-message ownership across concurrent sessions and restart, owner-only routing, exact mention sets, normalized member objects, concrete group-client command shape, dual Comate+Infoflow delivery with a shared canonical payload, early timeout rejection, reply-after-deadline timeout, durable handoff recovery, and post-decision timeout.

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -q
python3 -m unittest scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py scripts/tests/test_gates.py scripts/tests/test_orchestrator.py -q
python3 -m unittest discover -s scripts/tests -q
python3 -m py_compile scripts/state_store.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
```

Results: `7/7`, `61/61`, and `198 tests in 11.446s` all passed; compilation passed. The runtime scan found no `tom-autorelease` or Codex reference in Task 5 production files; the broad scan's only Codex hit is the pre-existing unrelated `scripts/install_links.py` destination list. The non-runtime `infoflow-gateway/README.md` was removed.

Current hashes:

```text
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/collaboration.py e206f66e2258c25d2efa71fd4c6a7db223bd69b03b57ad417d14b36c0737b254
scripts/approval_ledger.py b24cb4a70b780ba56e7f2098f4bc9a2d7fc85aae7364b125fa1157ff5d7d8890
scripts/orchestrator.py bc994f6230356b76e662146e3b0ceaeec5e66f322a22234baa7f77b72b3a994d
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py 75beceb6ec05e22cb4f56973ebc80c95c7bd2c640266936c7349cf9b6faf2ba2
scripts/tests/test_task5_safety.py 283a075b474ed9d4a8e9755db73c7524ce6aa0589e6bfdc77b60ba8f908782b6
infoflow-gateway/gateway.py 30870dbfda305da4f14d4328e183a913afb15748406691f059c940475dea97f3
```

Self-review: strict run-bound callers cannot fabricate G0; strict approvals require both channels, full member policies, authenticated responders and matching run/input; both write families claim StateStore ownership before external calls; late replies atomically resolve `TIMEOUT` and persist recovery handoff; owner-only routes use only the canonical project owner; Markdown mentions are exact-set validated and untrusted fields neutralize `@`. Legacy no-run ledger/session calls remain solely for Tasks 1-4 compatibility and do not authorize strict run-bound writes.

## Fix Round 2 RED Evidence

Before round-2 production edits, ran:

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -v
```

Observed the expected six contract failures: `Orchestrator.collaboration_session()` did not inject the ledger; a strict ledger row accepted an omitted `run_id`; a legacy approval advanced a run-bound G0 gate; `PendingApprovalGateway` had no StateStore-bound envelope lifecycle; `Orchestrator` exposed no wait/reply/heartbeat/timeout lifecycle; and changed approval evidence reused the original delivery intent. The focused suite reported `12` tests with `3` assertion failures and `3` missing-API errors. No production source was modified before this RED run.

The additional client-boundary RED test ran as:

```bash
python3 -m unittest scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_accepts_orchestrator_envelope_and_forwards_gateway_fields -v
```

It failed with `ValueError: APPROVAL_REQUEST_INVALID`, demonstrating that the real injected Infoflow client rejected the orchestrator's strict delivery envelope before any transport call. The client was then changed to validate and flatten that envelope at the gateway boundary.

## Fix Round 2 GREEN Evidence

Run-bound collaboration now uses the immutable `collaboration_binding` captured from the validated project profile during intake. The orchestrator always supplies its durable ledger, strict ledger responses require a matching run and authorized responder, `advance()` binds approval evidence to the current run, and the CLI requires both identity fields. Legacy direct ledger records remain compatible only outside a run-bound gate.

Approval deliveries persist a SHA-256 over the complete canonical channel envelope, including evidence and member policy, before reconciliation or send. Changed payloads now conflict at the durable StateStore claim. Gateway requests/replies require the run, approval, channel, input hash, and a member-policy-authorized responder; strict gateway timeouts require StateStore and persist the stable handoff. The orchestrator exposes fake-transport wait, reply, heartbeat, and timeout operations backed by the ledger and StateStore.

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -q
python3 -m unittest scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py scripts/tests/test_gates.py scripts/tests/test_orchestrator.py -q
python3 -m unittest discover -s scripts/tests -q
python3 -m py_compile scripts/state_store.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n -i 'tom-autorelease|codex' scripts/state_store.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py || true
```

Results: `12/12`, `67/67`, and `204` discovered tests passed. Both compile commands passed. The runtime reference scan produced no matches.

Current hashes:

```text
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/collaboration.py 0600214beb2ff57dbaf81c3e7dee10bf90e8a4ffb7a10bfe894c99d8858b4802
scripts/approval_ledger.py 3e663a3c9e7aa8d7de42f0ac3c72d477c348be5fe753e2a63e66f8cc76f0dc5a
scripts/orchestrator.py 6206c81c4a7903c3abb88be46b6512d0afa50f161fd38686892b4f0db4d066f6
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py 50f1351bf223e0a01b04c8518e3e4d219fee23b145bb02670d9b89bab306c76f
scripts/tests/test_task5_safety.py 72c4aeaa9473726fbc085cb9155a779038abc4ec25e71c31fc629cfcf25af8af
scripts/tests/test_approval_channels.py 8266aab004a64106606446128680354fab3d3056f4950a2f8beaf44339524983
scripts/tests/test_orchestrator.py ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99
infoflow-gateway/gateway.py 004b82c5bd6a3f3c2c8afdb62a279bfd30e8b8436ec52c45e150094e8c35e360
```

Final strict-title regression: the orchestrator-created session now accepts a caller card with an empty title and derives the group title solely from the immutable intake binding. The dedicated test and full discovery suite were rerun successfully (`1/1`, then `204` tests). Updated hashes after this final refactor:

```text
scripts/collaboration.py fe0175239213a43abe5987d67fc0c0d37d2f30e518b0b038daa6846182d8b19b
scripts/tests/test_task5_safety.py 9c76c3edc7ae1da3ba115f6d3775b06c51504bb37c2a2e22f36b438408c3f1bf
```

## Fix Round 3 RED Evidence

Before round-three production edits, ran:

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -v
```

Observed the expected four boundary failures: `Orchestrator.start()` rejected the canonical RequirementSnapshot argument; the gateway ignored its supplied `deadline_at`; an unsupported `codex` channel was accepted; and a reply without `decision` raised `KeyError` before the ledger could audit it. The focused suite reported `16` tests with `2` assertion failures and `2` errors. No production source was modified before this RED run.

## Fix Round 3 GREEN Evidence

`Orchestrator.start()` now accepts an optional RequirementSnapshot-style mapping. It writes a collaboration binding only when the snapshot has the requested canonical card ID, nonempty canonical title, SHA-256 content hash, and the validated profile role snapshot. Runs started without that snapshot remain usable for earlier phases but cannot prepare or create a strict G0 group. G0 input binding now includes the card content hash.

Strict ledger requests receive a durable explicit default deadline when none is supplied. The orchestrator sends that persisted deadline to both delivery channels. The Infoflow gateway requires the exact `infoflow` channel, complete Comate+Infoflow full-email policy, and the canonical deadline. Gateway records and every returned record are deep copied. Wait, reply, and explicit timeout all route expiry through the same StateStore-handoff transition.

Malformed Infoflow replies are routed through ledger audit. Missing/mismatched envelopes receive `APPROVAL_ENVELOPE_INVALID`; a syntactically valid `MAYBE` decision receives the ledger's `APPROVAL_DECISION_INVALID`. Neither form can disappear as pending or raise a callback `KeyError`.

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest scripts/tests/test_task5_safety.py -q
python3 -m unittest scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py scripts/tests/test_gates.py scripts/tests/test_orchestrator.py -q
python3 -m unittest discover -s scripts/tests -q
python3 -m py_compile scripts/state_store.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n -i 'tom-autorelease|codex' scripts/state_store.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py || true
```

Results: `16/16` round-three safety tests, `71/71` focused Task 5 tests, and `208` discovered tests passed. Both compile commands passed. The runtime-reference scan produced no matches.

Current hashes:

```text
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/collaboration.py 148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825
scripts/approval_ledger.py d82f60a877a6d83dd6c5b502c29b13497da7f347dd1f8d6cbaba26db606c8653
scripts/orchestrator.py 572fb01503ea0911f7eb20039d5deb5e0cfe5c9a50cd0385cc5baf7822ec0dba
scripts/evidence_gate.py 9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321
scripts/evidence_policy.py 7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py 50f1351bf223e0a01b04c8518e3e4d219fee23b145bb02670d9b89bab306c76f
scripts/tests/test_task5_safety.py 3881ff49e2c49512bf2a6274be2b74292e274d1d26ad78d91f267bb67276f469
scripts/tests/test_collaboration.py 6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea
scripts/tests/test_approval_channels.py 69f3c88505aecd4bf53707767aaf1003efbdb20a21b580f8558c07cc41365f2a
scripts/tests/test_gates.py 79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719
scripts/tests/test_orchestrator.py ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99
infoflow-gateway/gateway.py ed6b65232992834ee050174b146dade09b4eeadcfcf28fe8c8c6be651f1e8aa1
```

Final deadline-identity RED/GREEN: a retry with the same gateway request ID but a changed `deadline_at` initially replayed the existing request. The duplicate request identity now includes `deadline_at`, and the focused deadline/channel/immutability test plus the `208`-test discovery suite passed after the change. Updated hashes:

```text
scripts/tests/test_task5_safety.py 1fcbf2fa93cf91ae14daadcc33b551091ca102e099fa46b1a84ec360a3e0ea10
infoflow-gateway/gateway.py 339633e3824fcd05e58af8c65a7487f7863d318a0e1f7607a383671179db1fe9
```

## Fix Round 4 RED Evidence

Before round-four production edits, ran:

```bash
cd /Users/tom/Desktop/skills/tom-autodev
python3 -m unittest \
  scripts.tests.test_task5_safety.CollaborationSafetyTests.test_start_verifies_the_complete_cafe_snapshot_before_g0_uses_its_title \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_real_gateway_client_orchestrator_chain_accepts_pending_and_nested_reply_once \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_real_chain_reuses_timeout_handoff_and_audits_malformed_gateway_record \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_default_strict_deadline_is_exactly_ten_hours_in_ledger_and_gateway \
  -v
```

Observed the expected four production-boundary failures. A full snapshot whose title no longer matched its hash was accepted as a run and returned no failure reason. A real full `PendingApprovalGateway` PENDING record was falsely audited as `APPROVAL_ENVELOPE_INVALID`. A repeated timeout through the gateway/client/orchestrator chain returned `APPROVAL_ALREADY_RESOLVED` instead of the stable timeout handoff. The strict default deadline was exactly 30 minutes (`2026-08-11T00:30:00+00:00`) instead of the required ten hours (`2026-08-11T10:00:00+00:00`). The focused run reported `4` tests with `3` assertion failures and `1` expected contract error; no production file was modified before this RED run.

After the first GREEN and a `211/211` full discovery pass, a round-four self-review added three narrower fail-closed tests before the corresponding hardening edits. The focused RED run showed that `InfoflowApprovalClient.request()` accepted a partial request receipt instead of the shared gateway result, a shape-valid PENDING result with a changed member policy was accepted by the orchestrator, and a non-string gateway member raised `TypeError` instead of `APPROVAL_ENVELOPE_INVALID`. All three failed for those expected reasons.

## Fix Round 4 GREEN Evidence

The canonical RequirementSnapshot shape and hashing now live in `scripts/requirement_snapshot.py`. `CafeClient` and `Orchestrator.start()` call the same SHA-256 canonicalizer over every supplied snapshot field except `content_hash`; intake rejects incomplete, identity-mismatched, and hash-mismatched snapshots before creating a run. The production-path fixture contains all fields emitted by the real CafeClient adapter and uses a hand-checked literal hash.

The strict ten-hour default and one typed gateway result schema now live in `scripts/approval_contract.py`. `PendingApprovalGateway`, `InfoflowApprovalClient.request()/wait()`, and `Orchestrator.wait_infoflow_approval()` all publish, parse, or consume that schema. Full PENDING records produce no audit row. Accepted nested replies carry a stable `reply_id` and are applied once through durable orchestrator idempotency. TIMEOUT records carry the stable handoff and repeated polls reuse it. Partial, malformed, identity-mismatched, policy-mismatched, or deadline-mismatched records are audited fail-closed.

Focused repair GREEN:

```bash
python3 -m unittest \
  scripts.tests.test_task5_safety.CollaborationSafetyTests.test_start_verifies_the_complete_cafe_snapshot_before_g0_uses_its_title \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_real_gateway_client_orchestrator_chain_accepts_pending_and_nested_reply_once \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_real_chain_reuses_timeout_handoff_and_audits_malformed_gateway_record \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_default_strict_deadline_is_exactly_ten_hours_in_ledger_and_gateway \
  -v
```

Result: `4/4` passed. The supplementary fail-closed RED cases were then implemented and rerun as `3/3` passed.

Prior Task 5 suites and full discovery:

```bash
python3 -m unittest scripts/tests/test_task5_safety.py scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py scripts/tests/test_gates.py scripts/tests/test_orchestrator.py -q
python3 -m unittest discover -s scripts/tests -q
```

Results: `76/76` focused Task 5 tests and `213/213` discovered tests passed.

Static verification and dependency scan:

```bash
python3 -m py_compile scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n -i 'tom-autorelease|codex' scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
```

Both compilation commands exited `0`. The forbidden runtime dependency scan produced no matches (`rg` exit `1`, expected), and the import scan found only Python standard-library and local tom-autodev modules. No live call, business workspace command, BGW/XFlow command, or external write was run.

### Round 4 Changed Files

- `scripts/requirement_snapshot.py`: shared RequirementSnapshot type, full-shape validation, and canonical content hash.
- `scripts/approval_contract.py`: shared `36000`-second strict default, gateway result/reply/handoff TypedDicts, and strict parser.
- `scripts/clients/icafe_client.py`: uses the shared RequirementSnapshot hash function.
- `scripts/approval_ledger.py`: imports the single ten-hour strict default.
- `infoflow-gateway/gateway.py`: returns one complete immutable typed record for request/reply/wait/heartbeat/timeout, including stable reply and handoff identities.
- `scripts/clients/infoflow_approval_client.py`: validates complete request and wait results at the injected transport boundary.
- `scripts/orchestrator.py`: rejects invalid snapshots before intake; consumes full gateway records, binds them to ledger policy/deadline, deduplicates replies, and reuses timeout handoffs.
- `scripts/tests/test_task5_safety.py` and `scripts/tests/test_approval_channels.py`: full snapshot, actual three-component chain, exact deadline, malformed/binding mismatch, and complete fake-record coverage.

Current reviewed hashes:

```text
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/requirement_snapshot.py fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9
scripts/approval_contract.py 97019bb2dcb5f35c74589b2fe8d09935356502c7856f310a898834db540615be
scripts/collaboration.py 148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825
scripts/approval_ledger.py bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10
scripts/orchestrator.py 1f33021e3289751dcf8ce41546f8fc38bf1d9e1eaf27b5c70457d3353ef0cb44
scripts/evidence_gate.py 9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321
scripts/evidence_policy.py 7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d
scripts/clients/icafe_client.py 886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py cee73c79452a6a4ab68ce4391302bb6bc4e31779771292401718836dec9b3c00
scripts/tests/test_task5_safety.py b6acdd19115465d0d1689ef2433b69a65804e29d262acc792cd860e48c209411
scripts/tests/test_collaboration.py 6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea
scripts/tests/test_approval_channels.py 87bfa85bd1b22b8ed183cfd834ae3f12a3de1657bd11b95a78b3479df623da9e
scripts/tests/test_gates.py 79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719
scripts/tests/test_orchestrator.py ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99
infoflow-gateway/gateway.py de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0
```

### Round 4 Self-Review

- C1: complete CafeClient snapshots are required and recomputed through the same shared canonicalizer; arbitrary three-field snapshots and title/content/hash mismatches fail before run persistence.
- C4/N4: gateway, client, and orchestrator share one typed result. Full pending polls are inert, nested accepted replies are durable/idempotent, and timeout produces one stable handoff across both stores.
- I4: tests traverse the real `PendingApprovalGateway -> InfoflowApprovalClient -> Orchestrator` production path with only the external transport boundary faked; mismatch and malformed paths are covered.
- N5: the sole strict default is `36000` seconds, and the exact fixed ledger deadline is asserted equal to the gateway deadline.
- Previously addressed channel/run/responder/member, deadline immutability, delivery-intent, timeout, and malformed-decision behavior remains covered by the 76-test focused suite. No known round-three open finding remains.

## Fix Round 5 RED Evidence

Before round-five production edits, the focused request-result binding command was:

```bash
python3 -m unittest \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_binds_complete_pending_result_to_every_immutable_request_field \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_accepts_order_normalized_policy_and_normalized_deadline \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rejects_each_complete_mismatched_infoflow_delivery_receipt \
  -v
```

The complete PENDING record mismatches for `run_id`, `approval_id`, `input_hash`, canonical `member_policy`, and normalized `deadline_at` were accepted by `InfoflowApprovalClient.request()`. The invalid `channel` was rejected by the typed parser, but Orchestrator converted that deterministic response failure into `QUERY_REQUIRED` and reconciled. The other five mismatches were recorded as successful Infoflow deliveries, so the Orchestrator subtests had no failure reason. The semantically matching record with reversed member order and an equivalent UTC deadline passed, establishing the normalization compatibility that the fix had to preserve.

A separate record-boundary mutation removed only the Orchestrator pre-receipt check. Its raw complete record with a changed deadline was recorded and the focused test failed with a missing `APPROVAL_REQUEST_RESPONSE_INVALID`; restoring the check made the test pass. Finally, repeating each of the six mismatched canonical requests produced six RED failures because the second call returned `QUERY_REQUIRED`. This proved that a durable terminal failure replay was needed to make no second send or reconcile while retaining the stable error code.

## Fix Round 5 GREEN Evidence

`scripts/approval_contract.py` now owns the single `gateway_result_matches_request()` validator. It compares exact run, approval, channel, and input identities; deeply compares the complete two-channel member policy after sorting each member list; and parses both deadlines as timezone-aware values normalized to UTC. `InfoflowApprovalClient.request()` invokes it before returning a PENDING result. Orchestrator invokes the same validator before either StateStore or ledger receipt persistence, uses the ledger-canonical member policy for the outbound envelope, and durably replays deterministic invalid-result failures without sending or reconciling again.

Focused request-result GREEN:

```bash
python3 -m unittest \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_binds_complete_pending_result_to_every_immutable_request_field \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_accepts_order_normalized_policy_and_normalized_deadline \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rejects_each_complete_mismatched_infoflow_delivery_receipt \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rechecks_typed_infoflow_receipt_before_recording_it \
  -v
```

Result: `4/4` passed. The Orchestrator mismatch test covers all six immutable fields, invokes each canonical request twice, and asserts one transport request, zero reconcile calls, no Infoflow delivery receipt, PENDING/unresolved state, and `APPROVAL_DELIVERY_INCOMPLETE` for a Comate decision.

Final Task 5 and full discovery:

```bash
python3 -m unittest scripts/tests/test_task5_safety.py scripts/tests/test_collaboration.py scripts/tests/test_approval_channels.py scripts/tests/test_gates.py scripts/tests/test_orchestrator.py -q
python3 -m unittest discover -s scripts/tests -q
```

Results: `80/80` focused Task 5 tests and `217/217` discovered tests passed.

Static verification and forbidden dependency scan:

```bash
python3 -m py_compile scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n -i 'tom-autorelease|codex' scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
rg -n '^(from|import) ' scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
```

Both compilation commands exited `0`. The forbidden runtime dependency scan produced no matches (`rg` exit `1`, expected). The import scan contained only Python standard-library and local tom-autodev modules. No live call, business workspace command, BGW/XFlow command, or external write was run.

### Round 5 Current Hashes

```text
task-5-brief.md 616384db438a39da87e5d9e351521e39342f3a11e2e6eb2a88999d119d2635db
task-5-rereview-round4.md 0731713fea6738d17c63722f14bfcaa55f1aef9433248ea7160da7adccf3024d
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/requirement_snapshot.py fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9
scripts/approval_contract.py d4b99f762a39b76fb9fc7f75b825df8da163a03767ae5cc4285fd5a5f0f52dbf
scripts/collaboration.py 148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825
scripts/approval_ledger.py bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10
scripts/orchestrator.py 40a3c8e5523aa214eaff5faa4980a45f92e3a97dd9bb928e53a2698553627ce6
scripts/evidence_gate.py 9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321
scripts/evidence_policy.py 7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d
scripts/clients/icafe_client.py 886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py 175fe9aee20c796288a4944c990ca60465ccba66ef190d0d9c2a6ca94f685e7f
scripts/tests/test_task5_safety.py 996ff633871983fa83200451ad5248d3ae461d1b476deb41c527198bb7f328ca
scripts/tests/test_collaboration.py 6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea
scripts/tests/test_approval_channels.py 5182449e44ba3329b329bc48ad5302f4162f2f064103687c32dfeeef66cf1ec3
scripts/tests/test_gates.py 79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719
scripts/tests/test_orchestrator.py ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99
infoflow-gateway/gateway.py de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0
```

### Round 5 Self-Review

- Critical N6: every complete request result is now bound to all six immutable outbound fields before return and again before receipt persistence. A complete misrouted result cannot satisfy strict dual delivery.
- C3 regression: deterministic invalid results retain the pre-send durable intent, write no delivery receipt, and replay a durable stable failure on identical retries. Changed envelopes still conflict on the existing canonical payload SHA-256; unknown transport failures retain query/reconcile-only behavior.
- I4: direct client tests cover each field independently and a normalized matching success. The Orchestrator tests traverse `InfoflowApprovalClient -> Orchestrator`, prove no receipt/no resolution/no second external operation for every mismatch, and independently cover the Orchestrator record boundary with a raw typed client.
- Previously addressed C1, C2, C4, C5, I1, I2, I3, N1, N2, N3, N4, and N5 production files remain unchanged except the scoped canonical policy source in Orchestrator. The final `80/80` Task 5 suite preserves snapshot hashing, collaboration ownership/routing, strict channel/run/responder/input checks, malformed reply audit, exact ten-hour deadline, nested reply idempotency, and stable timeout handoff coverage.

## Exception Round 6 RED Evidence (N7)

The authorized exception round is limited to N7: a complete, six-field-matching initial PENDING result must be clean before it can count as delivery. The new RED command was:

```bash
python3 -m unittest \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_rejects_each_matching_pending_gateway_error \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rejects_each_matching_pending_gateway_error_before_receipt \
  -v
```

The four gateway-produced pending error codes (`APPROVAL_ENVELOPE_INVALID`, `APPROVAL_INPUT_MISMATCH`, `APPROVAL_RESPONDER_UNAUTHORIZED`, and `APPROVAL_DECISION_INVALID`) all passed the existing parser, six-field binding, and `status == PENDING` checks. The client accepted all four instead of raising `APPROVAL_REQUEST_RESPONSE_INVALID`. The raw Orchestrator path persisted each as an Infoflow delivery; its second identical call then replayed successful state, and a valid Comate response could authorize. These expected failures establish N7 at both request boundaries.

During GREEN verification, one pre-existing nested-reply test exposed a real-clock fixture race: its fixed gateway clock was `2026-08-11T00:00Z` but the ledger observed the host clock after the one-hour deadline and correctly timed out. The test-only deadline was extended to 24 hours after the fixed clock; no production timeout behavior changed.

## Exception Round 6 GREEN Evidence

`approval_contract.is_clean_initial_pending_result()` is the shared initial-request predicate. It requires exactly `status == "PENDING"`, `reason_code is None`, `reply is None`, and `handoff is None`. Both `InfoflowApprovalClient.request()` and `Orchestrator._deliver_approval_channel()` apply it after complete parsing and six-field binding. Non-clean records enter the round-five durable `APPROVAL_REQUEST_RESPONSE_INVALID` failure marker path, so they write no Infoflow ledger receipt, cannot authorize Comate, and replay the same stable error without resend or reconcile.

Focused exception and preserved-boundary GREEN:

```bash
python3 -m unittest \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_rejects_each_matching_pending_gateway_error \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rejects_each_matching_pending_gateway_error_before_receipt \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_binds_complete_pending_result_to_every_immutable_request_field \
  scripts.tests.test_approval_channels.ApprovalLedgerChannelTests.test_infoflow_client_accepts_order_normalized_policy_and_normalized_deadline \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rejects_each_complete_mismatched_infoflow_delivery_receipt \
  scripts.tests.test_task5_safety.ClientAndOrchestratorSafetyTests.test_orchestrator_rechecks_typed_infoflow_receipt_before_recording_it \
  -v
```

Result: `6/6` passed. The complete Task 5 suites then passed `82/82`; full discovery passed `219/219`.

Static verification:

```bash
python3 -m py_compile scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
python3 -m compileall -q scripts infoflow-gateway
rg -n -i 'tom-autorelease|codex' scripts/state_store.py scripts/requirement_snapshot.py scripts/approval_contract.py scripts/collaboration.py scripts/approval_ledger.py scripts/orchestrator.py scripts/evidence_gate.py scripts/evidence_policy.py scripts/clients/icafe_client.py scripts/clients/infoflow_group_client.py scripts/clients/infoflow_approval_client.py infoflow-gateway/gateway.py
```

Both compile commands exited `0`. The forbidden runtime dependency scan produced no matches (`rg` exit `1`, expected). No live call, business workspace command, BGW/XFlow command, or external write was run.

### Exception Round 6 Hashes

```text
task-5-rereview-round5.md af1115f5548b87bad10e64d651f2a33c975bdc586962f799edcbcaa3306ec7be
scripts/state_store.py 79ac48d4f7066cae6c8ded8d5a60dbb48668117aac183a74975fd8a01368b25c
scripts/requirement_snapshot.py fce7c1c8b260a3326e59c7eac57dcbda666d567bdd9fcc5a6e19b0d4f7dfc9e9
scripts/approval_contract.py 951a79e2296dd086b2e3c762546cbcfef9b850a9421290637fe983a71e58591a
scripts/collaboration.py 148e952026443e3f1efa1531b6d8a282a84be3b3db98ee78156e33e1c900f825
scripts/approval_ledger.py bf7c96505e0f0fe106763e6fd9b6cfd4dc6a045a297fc9229104d3588a821d10
scripts/orchestrator.py 616e29153a0abcfc5ef81335d1bbd638997c218062febf362b320c702c5e57c8
scripts/evidence_gate.py 9e13f8b26c00ee131a1d49d4717c59b1299a3c073a863ab3d3ce6d23d61c1321
scripts/evidence_policy.py 7c50a7c259a27f883b5a1a698eb16fe42382ada63909968853594f41f832951d
scripts/clients/icafe_client.py 886878dd8cf96f48675a57537a1732fddd0d244c7ddff9a8771dee8336ddfec1
scripts/clients/infoflow_group_client.py cf8d5aecd9531ca59ccdd4e08a84314aad897675654ed4c3288e8669f6bacd68
scripts/clients/infoflow_approval_client.py 76e4ea3e7bc37544f486b283490bc8125866fba1d08964d94c8fa3ae796ba76e
scripts/tests/test_task5_safety.py f58eecdf24e3b3cd0604d4faf9474fc32d021dc482eefbf83126f3e93ce6b93e
scripts/tests/test_collaboration.py 6f1d6dd95f892c607a048623fa8c0f8a120ba64fb5e55f222daa172cc5fc27ea
scripts/tests/test_approval_channels.py 70fb1659be53bcc9b3c8d08d605cbbeb94d8dfccac699b986b8d88abd8dcffbe
scripts/tests/test_gates.py 79c2f9bbbb18b3a0a8104e04bd9b3d4239d9f66bf32a6bf9930310935e9da719
scripts/tests/test_orchestrator.py ab6948edfe50e503763221307d392a98ac0201dbdadedf75a0063f1dcddc9c99
infoflow-gateway/gateway.py de7cb902cf5be0b5170e91af4ca4b0378d1e70bfc35f2462bc83548c7dc461a0
```

### Exception Round 6 Self-Review

- N7: the clean initial predicate is centralized and applied before both client return and any StateStore/ledger receipt. All four gateway error-bearing PENDING records fail closed.
- C3/N6 preserved: failure markers remain durable and stable; identical retries perform no second send/reconcile, changed envelopes still conflict before failure replay, and normalized six-field matching success remains accepted.
- I4: direct client and raw Orchestrator tests cover every gateway-produced pending error code, no Infoflow receipt, unresolved approval, Comate authorization block, stable retry, and zero reconcile.
- C1, C2, C4, C5, I1, I2, I3, N1, N2, N3, N4, and N5 remain covered by the final `82/82` Task 5 suite. The only unrelated test edit removes the documented fixed-clock deadline race; no production scope was broadened.
