# Task 4 Re-review: Fix Round 1

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- Original findings addressed: 5
- Original findings not addressed: 2
- New Critical findings: 0
- New Important findings: 1

This was a static re-review of the current Task 4 implementation and tests against `task-4-review.md` and the appended fix-round report. Per instruction, none of the recorded tests were rerun. No live iCafe/KU write, BGW/XFlow command, or production worktree operation was performed.

## Finding Status

### C1: ADDRESSED

Completed create receipts now re-query and resume publishing rather than calling `create-doc` again ([scripts/clients/ku_client.py:192](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:192)). Unknown creates query the configured repository and exact parent, then reconcile the deterministic title/content marker ([scripts/clients/ku_client.py:202](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:202), [scripts/clients/ku_client.py:441](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:441)). Incomplete pagination fails closed ([scripts/clients/ku_client.py:513](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:513)). The focused adapter and full-sync tests now cover unknown create, unknown child publish, and index failure retries while asserting exactly one `create-doc` ([scripts/tests/test_adapters.py:698](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:698), [scripts/tests/test_knowledge_sync.py:381](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_knowledge_sync.py:381), [scripts/tests/test_knowledge_sync.py:456](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_knowledge_sync.py:456), [scripts/tests/test_knowledge_sync.py:516](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_knowledge_sync.py:516)).

### C2: ADDRESSED

An exact existing index block now resolves a pending edit receipt and always proceeds through `_publish()` ([scripts/clients/ku_client.py:283](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:283)). Pending publish reconciliation returns success only when the remote document is published and the exact required entry is present ([scripts/clients/ku_client.py:394](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:394), [scripts/clients/ku_client.py:610](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:610)). `KnowledgeSync` also withholds its final receipt while a lower external intent remains pending ([scripts/knowledge_sync.py:151](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:151)). The new tests exercise both edit-unknown and publish-unknown recovery and assert no pending intents remain ([scripts/tests/test_adapters.py:779](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:779), [scripts/tests/test_adapters.py:818](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:818)).

### I1: NOT ADDRESSED

Comment replay, changed-content conflict detection, completed status replay, and close ordering were fixed ([scripts/clients/icafe_client.py:150](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:150), [scripts/clients/icafe_client.py:231](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:231), [scripts/clients/icafe_client.py:397](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:397)). However, `close()` still derives its status idempotency key from the status returned by a fresh snapshot ([scripts/clients/icafe_client.py:383](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:383), [scripts/clients/icafe_client.py:391](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:391)).

On the first close, the key is `<old-status>:<terminal-status>`. On an exact replay after the transition, the snapshot returns the terminal status, so the method looks for a different `<terminal-status>:<terminal-status>` receipt. It finds none, sees `already_target`, and returns `ICAFE_STATUS_CHANGED` ([scripts/clients/icafe_client.py:400](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:400)). This also makes a partial close unrecoverable through `close()`: if status succeeds and comment creation is unknown/fails, retry stops before comment reconciliation. There is no close-level intent/result keyed by the approved input hash. The new close tests cover unreachable-before-comment and one successful call, but not successful replay or status-success/comment-unknown recovery ([scripts/tests/test_adapters.py:456](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:456), [scripts/tests/test_adapters.py:509](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:509)).

The KU URL check also remains only `startswith("https://")` ([scripts/clients/icafe_client.py:370](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:370)); the tests continue to authorize `ku.example.test`. Approval action/decision/input-hash lookup is authentic, but the approved link is not verified as a canonical KU URL.

### I2: ADDRESSED

KU and iCafe writes now persist definite failures with their stable reason codes and reserve `QUERY_REQUIRED` for timeout/process/invalid-JSON uncertainty ([scripts/clients/ku_client.py:247](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:247), [scripts/clients/ku_client.py:331](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:331), [scripts/clients/icafe_client.py:282](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:282)). Exit-code-one parsing distinguishes iCafe `auth_failed` from Cobra input failure ([scripts/cli_transport.py:156](/Users/tom/Desktop/skills/tom-autodev/scripts/cli_transport.py:156)). Focused tests cover definite KU/iCafe permission failures and the exit-code mapping.

### I3: ADDRESSED

Remote document queries now validate `docGuid`, configured `repositoryGuid`, and a canonical `ku.baidu-int.com` URL whose final components match the repository and document ([scripts/clients/ku_client.py:527](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:527), [scripts/clients/ku_client.py:624](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:624), [scripts/clients/ku_client.py:771](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:771)). Index verification requires one exact Markdown entry and one marker rather than marker presence alone ([scripts/clients/ku_client.py:791](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:791)), and publish verification requires the exact content/entry plus `initType=0`.

### I4: ADDRESSED

Snapshots compare returned space/sequence with the requested card and validate required title/body/status/type/people/field/metadata shapes before constructing and hashing the snapshot ([scripts/clients/icafe_client.py:85](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:85), [scripts/clients/icafe_client.py:598](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:598)). The added test covers identity mismatch and malformed required shapes ([scripts/tests/test_adapters.py:178](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:178)).

### I5: NOT ADDRESSED

The fix adds the correct hard-bound BGW/XFlow repo/parent pairs, deterministic per-run root creation, and an Orchestrator factory ([scripts/knowledge_sync.py:13](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:13), [scripts/knowledge_sync.py:39](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:39), [scripts/knowledge_sync.py:208](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:208), [scripts/orchestrator.py:71](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:71)). The binding is still not to the exact profile approved at run start.

`start()` records `profile_hash` in the intake event ([scripts/orchestrator.py:34](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:34)), but `knowledge_sync()` reloads the current file and never compares its bytes with that recorded hash or verifies its `project_id` against the recorded project ([scripts/orchestrator.py:83](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:83), [scripts/orchestrator.py:90](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:90)). Replacing `bgw.yaml` with another valid profile, including a valid XFlow profile, can therefore redirect an already-started BGW run to the XFlow parent. The factory test covers an unchanged file only ([scripts/tests/test_orchestrator.py:88](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:88)). Recompute and compare the recorded profile hash before constructing any adapter, and reject a project-ID mismatch.

## New Important Finding

### N1. A run-scoped `KnowledgeSync` accepts a different `run_id`, splitting intents and recovery across runs

`KnowledgeSync.from_profile()` binds the KU and iCafe clients to its construction-time `run_id` ([scripts/knowledge_sync.py:69](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:69), [scripts/knowledge_sync.py:76](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:76)), but the object does not retain that expected ID. `publish_phase()` accepts any non-empty `run_id` and uses it for the root body, phase intent, idempotency key, and lower-pending scan ([scripts/knowledge_sync.py:88](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:88), [scripts/knowledge_sync.py:112](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:112), [scripts/knowledge_sync.py:151](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:151), [scripts/knowledge_sync.py:218](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:218)). The child, index, and comment clients nevertheless persist their intents under the construction-time run.

A caller can therefore construct sync for run A and invoke `publish_phase("run-B", ...)`: the top-level phase intent and result belong to B, while all remote-write intents and receipts belong to A. The lower-intent guard scans B and cannot see pending lower writes under A, defeating recovery ownership and potentially allowing a success receipt for the wrong run. Store the construction-time run ID on `KnowledgeSync`, reject mismatches before resolving/creating the root, and add a zero-call regression test.

## Assessment

The two original Critical KU recovery failures are fixed, and the implementation now has materially stronger verification and targeted retry coverage. Acceptance still fails because close/cancel is not end-to-end idempotent, a run can be rebound to a changed project profile, and `publish_phase()` can split one operation's durable state across two run IDs. No additional Critical regression was found.
