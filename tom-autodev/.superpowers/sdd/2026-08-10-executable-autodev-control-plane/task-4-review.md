# Task 4 Review: Real iCafe, KU and Knowledge Synchronization

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- Critical findings: 2
- Important findings: 5

This was a static review. Per the review instruction, the focused and full-suite test runs recorded in `task-4-report.md` were not rerun. No live iCafe/KU write, BGW/XFlow build or test, or production worktree operation was performed.

## Critical Findings

### C1. A phase retry can create a second supposedly immutable KU child

Child discovery consults only the root-index marker ([scripts/clients/ku_client.py:69](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:69)). After a successful `create-doc`, the adapter writes the create receipt before publishing the child ([scripts/clients/ku_client.py:140](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:140)); the root index is not updated until `create_artifact()` has returned success to `KnowledgeSync` ([scripts/knowledge_sync.py:59](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:59)). Consequently, either of these ordinary sequences leaves a real child with no root marker: child publish fails after the create receipt, or child creation/publish succeeds and the subsequent index update fails.

On retry, the root query still finds no marker and `_pending(create_key)` finds no create intent because that intent already has a receipt ([scripts/clients/ku_client.py:93](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:93)). The code then executes `create-doc` again ([scripts/clients/ku_client.py:109](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:109)). The child marker is embedded in document content, but there is no repository/parent query that can discover the orphaned matching child.

This violates immutable-child idempotency and the query-before-repeat recovery rule. Reconciliation must be based on a remotely queryable deterministic identity independent of the root index, or a durable verified create receipt must be replayable through publish/index without creating again. Add end-to-end fake-transport retries for failures after child create, after child publish, and during index update.

### C2. Root-index recovery can report success while edit/publish intents remain pending and the index is unpublished

`update_index()` returns a duplicate success as soon as it sees a matching marker ([scripts/clients/ku_client.py:188](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:188)). That branch does not reconcile a pending edit intent, does not call `_publish()`, and does not require the queried version to have `initType=0`; it simply returns the latest version ([scripts/clients/ku_client.py:192](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:192)).

Thus, if `edit-content` applied remotely but its result was unknown, or if edit verification succeeded and `publish-doc` was unknown/failed, the next call sees the marker and bypasses the pending-intent reconciliation in `_publish()` ([scripts/clients/ku_client.py:276](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:276)). `KnowledgeSync` accepts that duplicate index receipt, posts the iCafe comment, and persists a successful phase receipt ([scripts/knowledge_sync.py:69](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:69), [scripts/knowledge_sync.py:108](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:108)), while the lower-level intent remains pending and the root may still be draft-only.

This directly contradicts the report's claim that unknown writes remain pending without allowing completion. Existing-marker handling must first reconcile matching pending edit/publish intents, verify the exact entry, and prove a published remote version before returning success.

## Important Findings

### I1. Status and close operations are not idempotent, and close writes its comment before proving reachability

`update_status()` recognizes an already-reached target only when the matching intent is still pending ([scripts/clients/icafe_client.py:213](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:213)). After the receipt is committed, an exact replay sees the target status but falls through to `ICAFE_STATUS_CHANGED` ([scripts/clients/icafe_client.py:217](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:217)); there is no completed-receipt lookup by idempotency key. `close()` therefore also fails on replay: its comment is found idempotently, but its completed status transition is not.

`close()` additionally posts the close/cancel reason before `update_status()` checks the current state and `next-statuses` ([scripts/clients/icafe_client.py:325](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:325)). An unreachable terminal state can leave a misleading close comment even though the close fails. The ledger lookup does correctly bind action, effective decision, and canonical input hash ([scripts/clients/icafe_client.py:311](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:311)), but reachability must be proven before the first close write and completed receipts must be replayable. Also, an existing comment marker is accepted without checking that its content hash matches the current request ([scripts/clients/icafe_client.py:143](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:143)).

### I2. Deterministic write failures are mislabeled as unknown results

The transport and `_invoke()` methods derive stable auth, permission, missing-object, invalid-input, and business reason codes, but KU create/edit/publish replace every write failure with `QUERY_REQUIRED` ([scripts/clients/ku_client.py:125](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:125), [scripts/clients/ku_client.py:240](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:240), [scripts/clients/ku_client.py:303](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:303)). iCafe status update does the same ([scripts/clients/icafe_client.py:262](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:262)). A definite `PERMISSION_DENIED` or invalid business response is therefore persisted as an unresolved intent instead of returning its stable reason and resolving the non-write.

At the process layer, exit code 1 is always collapsed to `AUTH_OR_INPUT_REQUIRED` ([scripts/cli_transport.py:173](/Users/tom/Desktop/skills/tom-autodev/scripts/cli_transport.py:173)), even though the real CLI documents an `auth_failed` JSON discriminator that can distinguish authentication from Cobra input errors. Preserve known reason codes; reserve `QUERY_REQUIRED` for timeout/transport outcomes where remote application is genuinely uncertain.

### I3. KU post-write verification does not prove repo/URL identity or the exact root entry

Create validates `repositoryGuid` and URL only from the write response ([scripts/clients/ku_client.py:127](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:127)). The subsequent remote verifier retains only `docGuid`, text, version and `initType` ([scripts/clients/ku_client.py:335](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:335)); it cannot prove the queried document still belongs to the configured repository or that its canonical URL corresponds to that repo/doc. The URL predicate accepts any query-free HTTPS URL and does not bind its final components to `repo_id`/`doc_id` ([scripts/clients/ku_client.py:474](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:474)).

For the root index, post-edit verification only checks that the marker occurs somewhere ([scripts/clients/ku_client.py:242](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:242)); the link/title/hash line can be missing, altered, duplicated, or placed incorrectly and still pass. This does not satisfy the required doc ID, repo ID, URL/version, canonical content-hash, and exact-entry verification after every create/edit/publish.

### I4. `RequirementSnapshot` can certify the wrong remote card under the requested canonical ID

After `card get`, the adapter requires exactly one card but never compares that card's returned `spacePrefixCode` and `sequence` with the requested values ([scripts/clients/icafe_client.py:85](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:85)). It then assigns the caller-derived ID as `canonical_card_id` and hashes the resulting object ([scripts/clients/icafe_client.py:89](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:89), [scripts/clients/icafe_client.py:123](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icafe_client.py:123)). A malformed, stale, or mismatched CLI response can therefore produce a valid-looking immutable hash for the wrong requirement.

Verify the returned space/sequence and required field shapes before hashing. Tests currently assert only a matching fake response and do not cover identity mismatch, malformed metadata, or attachment/link extraction from realistic CLI payloads ([scripts/tests/test_adapters.py:100](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:100)).

### I5. KU synchronization is not wired to the project profile or a per-run root document

`KuClient` accepts an arbitrary standalone `repo_id` ([scripts/clients/ku_client.py:25](/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ku_client.py:25)), while `KnowledgeSync` independently accepts an arbitrary `parent_doc_id` and uses that same document both as the phase-child parent and as the index to edit ([scripts/knowledge_sync.py:18](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:18), [scripts/knowledge_sync.py:59](/Users/tom/Desktop/skills/tom-autodev/scripts/knowledge_sync.py:59)). No adapter factory/orchestrator path selects the KU source from the validated project profile, binds repo and parent together, checks the BGW/XFlow target, or creates the per-run root described by the design.

If the profile's parent ID is passed directly, phase entries are appended to the shared project parent rather than to a run-specific root. If a separately created root is intended, this task supplies no creation/receipt/wiring path for it. The tests conceal the gap by constructing clients with unrelated `repo-1`/`root-1` values ([scripts/tests/test_adapters.py:484](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:484), [scripts/tests/test_knowledge_sync.py:75](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_knowledge_sync.py:75)).

## Checks That Pass

- The reviewed iCafe argv shapes match the installed CLI documentation: final-hyphen card parsing, `card get --brief`, smart-find only for an empty explicit ID, comment get/create, current/next status, and guarded `card update`. No card creation, physical delete,正文 overwrite, or `--no-check-status` path was found.
- The first-use version/login preflight is present, and command-specific iCafe `code=200` versus `status=200` and KU `returnCode=200`/non-false-success checks are implemented.
- `CliTransport` uses argv arrays, an explicit timeout, JSON parsing, bounded hashed stderr diagnostics, and redacts the required content/credential flags. Reviewed durable intent/receipt payloads contain hashes and identifiers rather than raw comment/Markdown content or environment values.
- All reviewed external writes create a `StateStore.intent` before the CLI invocation, and verified happy paths write canonical `ku:`/`icafe:` evidence receipts afterward.
- No live-service calls, BGW/XFlow commands, production worktree paths, Codex approval support, or non-fake adapter transports appear in the Task 4 tests.

## Test Assessment

The recorded green runs exercise the intended command shapes and happy paths, but they do not cover the blocking cross-call sequences: successful child followed by index failure and retry; child publish failure and retry; edit-applied/publish-unknown root reconciliation; or a successful phase response with lower-level pending intents. They also omit completed status/close replay, close reachability before comment, comment-key reuse with changed content, known permission/business failures, mismatched card identity, exact root-entry corruption, remote repo/URL mismatch, and real project-profile/run-root binding. Those gaps explain why the suite can pass while the two critical recovery violations remain.
