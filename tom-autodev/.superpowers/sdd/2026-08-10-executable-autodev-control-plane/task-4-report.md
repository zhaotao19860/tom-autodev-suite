# Task 4 Implementation Report

## Status

Complete. The control plane now has bounded argv-based CLI execution, real iCafe and
KU command adapters, durable intent/verified-receipt handling for every external
write, and ordered phase synchronization across immutable KU children, the run index,
and an idempotent iCafe comment.

## Documentation Reviewed

- Task 4 brief and the Task 4 section of the approved implementation plan.
- Control-plane design sections for project KU targets, iCafe, KU, persistence,
  idempotency, recovery, and adapter acceptance.
- Real iCafe Skill documentation for version/login preflight, card get/smart-find,
  comments, current/next status, update, response codes, and error mapping.
- Real KU Skill documentation for `BAIDU_CC_USERNAME`, `query-content`,
  `query-version`, `create-doc`, MDSL `edit-content`, and `publish-doc`.
- Task 3 `StateStore`, canonical evidence-reference, and `Recovery` contracts.

## Changed Files

- `scripts/cli_transport.py`
  - Added argv-only subprocess execution, explicit timeouts, JSON parsing, exit and
    business-code validation, bounded stderr hashing, environment export, and
    redacted diagnostic argv.
- `scripts/clients/icafe_client.py`
  - Added one-time version/login preflight, final-hyphen card parsing, unique-only
    smart-find fallback, complete immutable requirement snapshots, idempotent comment
    reconciliation, guarded status transitions, and approved close/cancel semantics.
- `scripts/clients/ku_client.py`
  - Added profile-targeted KU create/query/edit/publish operations, immutable child
    markers, exact root-index MDSL append, post-write content/version verification,
    and published-version verification (`initType=0`).
- `scripts/knowledge_sync.py`
  - Added ordered child creation, index update, iCafe comment, canonical evidence
    validation, durable synchronization receipt, and incomplete-phase failure paths.
- `scripts/persistence_policy.py`
  - Extended the existing canonical evidence grammar with
    `icafe:<card>/<remote-object>` references.
- `scripts/tests/test_adapters.py`
  - Added fake-runner/fake-transport coverage for CLI, iCafe, KU, persistence,
    idempotency, verification, and close/cancel behavior.
- `scripts/tests/test_knowledge_sync.py`
  - Added phase synchronization, evidence, raw-content confinement, ordering, and
    recovery tests.

`scripts/orchestrator.py` was not changed. Task 4 does not introduce a new CLI command
or state transition, and constructing phase actions belongs to the later phase-action
task. The adapters expose the required interfaces without widening current
orchestrator behavior.

## Contract Details

- iCafe `card get` and `card update` require `code=200`; comments, smart-find,
  current-status and next-statuses require `status=200`.
- No adapter creates requirement cards, overwrites requirement正文, uses
  `--no-check-status`, or calls a physical delete operation.
- Close/cancel requires a configured terminal status, reason, canonical HTTPS KU URL,
  and an `ICAFE_CLOSE` ledger record approving the exact canonical input hash.
- KU create, edit, and publish each reserve a durable intent before execution. A
  receipt is written only after content and version re-query. Publish additionally
  requires the latest version to be a published version.
- Child Markdown and comment content are passed only through redacted argv positions.
  Durable intent/receipt payloads contain hashes, IDs, safe canonical URLs, versions,
  and canonical evidence references, not raw content or stderr.
- Unknown write results remain pending. `Recovery.resume()` consequently returns
  `QUERY_REQUIRED`, and repeat calls query/reconcile rather than repeating the write.

## TDD Evidence

Initial RED command:

```text
python3 -m unittest scripts.tests.test_adapters scripts.tests.test_knowledge_sync -v
```

Result: exit 1. Of 28 tests, the six pre-existing adapter tests passed. Fifteen tests
failed and seven errored specifically because `CliTransport`, `KuClient`,
`KnowledgeSync`, and the expanded `CafeClient` interfaces were absent.

KU failure-path RED command:

```text
python3 -m unittest scripts.tests.test_adapters.KuClientTests -v
```

Result: exit 1, 8/8 expected failures before `KuClient` existed. The cases covered
create, query, edit, publish, immutable conflicts, intent ordering, and verification.

Contract-audit RED command:

```text
python3 -m unittest scripts.tests.test_adapters.CafeClientTests scripts.tests.test_adapters.KuClientTests -v
```

Result: exit 1 with three expected gaps: missing smart-find fallback, close approval
not action-bound, and draft KU versions incorrectly accepted as published receipts.

Focused GREEN command before the final contract additions:

```text
python3 -m unittest scripts.tests.test_adapters scripts.tests.test_knowledge_sync -v
```

Result: 32 tests passed.

Expanded Cafe/KU GREEN command:

```text
python3 -m unittest scripts.tests.test_adapters.CafeClientTests scripts.tests.test_adapters.KuClientTests -v
```

Result: 21 tests passed. The approved close/cancel end-to-end fake test also passed
individually after being added as coverage for the already implemented path.

## Verification

Pre-report full control-plane command:

```text
python3 -m unittest discover -s scripts/tests -v
```

Result: 147 tests passed in 11.448 seconds.

Fresh post-report verification:

```text
python3 -m unittest scripts.tests.test_adapters scripts.tests.test_knowledge_sync -v
python3 -m unittest discover -s scripts/tests -v
```

Result: 37 focused Task 4 tests passed in 0.124 seconds, followed by 148 full
control-plane tests passed in 11.035 seconds.

Compile and import checks passed for all Task 4 production and test modules. The
prohibited-pattern scan found no shell execution, delete operation, Codex support,
raw stderr persistence, or production use of `--no-check-status`.

## Safety and Concerns

- All iCafe and KU adapter tests use fake transports. No live service write was made.
- No BGW/XFlow build, test, repository, or worktree command was run.
- The workspace is not a Git repository, so Git diff/status evidence is unavailable.
- Live read-only preflight and real profile wiring remain separate acceptance work;
  this task deliberately did not perform them.

## Fix Round 1 (2026-08-10)

This round addresses every finding in `task-4-review.md`. It supersedes the original
report statement that `scripts/orchestrator.py` was unchanged: the orchestrator now
constructs knowledge synchronization from the exact validated profile path and
requirement ID recorded in the run's first event.

### Review Finding Resolution

- **C1:** KU child creation now queries the configured repository under the exact
  parent, reconciles pending creates by deterministic title plus embedded marker,
  and replays completed create receipts through publish/index without creating a
  second child. End-to-end fake-transport retries cover unknown create, unknown
  child publish, and unknown index edit.
- **C2:** Exact existing index entries reconcile pending edit and publish intents,
  require one exact entry, and require a remotely published `initType=0` version.
  `KnowledgeSync` additionally refuses its final receipt while any lower external
  intent for the run remains pending.
- **I1:** Completed comment/status receipts replay without remote calls; comment
  marker reuse verifies exact content; close proves target reachability and performs
  the verified terminal transition before posting its reason/link comment.
- **I2:** Definite auth, permission, missing-object, invalid-input, and business
  failures keep their stable reason codes and receive failure receipts. Only
  transport-unknown outcomes remain pending as `QUERY_REQUIRED`. Exit code 1 maps
  iCafe `auth_failed` JSON to `AUTH_REQUIRED` and Cobra usage errors to
  `INVALID_INPUT`.
- **I3:** KU queries verify document ID, repository ID, canonical repo/doc URL,
  content, version, publication state, and exact single root-index entry.
- **I4:** iCafe snapshots reject mismatched remote card identity and malformed
  required card shapes.
- **I5:** BGW and XFlow are bound to repository `sX0BTOBWJX` and parents
  `I15ClP2KW4ZGAK` / `meQ-Acjg0K09Xr`. Each run has one deterministic mutable root;
  immutable phase children are created and indexed only beneath that root.

### Changed Files

- `scripts/state_store.py`
- `scripts/cli_transport.py`
- `scripts/clients/icafe_client.py`
- `scripts/clients/ku_client.py`
- `scripts/knowledge_sync.py`
- `scripts/orchestrator.py`
- `scripts/tests/test_state_and_artifacts.py`
- `scripts/tests/test_adapters.py`
- `scripts/tests/test_knowledge_sync.py`
- `scripts/tests/test_orchestrator.py`

### Fix-Round TDD Evidence

The new state lookup, CLI mapping, iCafe identity/replay/conflict/failure/close, KU
reconciliation/identity/exact-entry/publication, run-root, profile-binding, and retry
tests were introduced as focused regressions before their production paths. The
profile/orchestrator RED command was:

```text
python3 -m unittest \
  scripts.tests.test_knowledge_sync.KnowledgeSyncTests.test_profile_bound_sync_creates_one_run_root_and_indexes_only_that_root \
  scripts.tests.test_knowledge_sync.KnowledgeSyncTests.test_from_profile_enforces_exact_bgw_and_xflow_ku_targets \
  scripts.tests.test_knowledge_sync.KnowledgeSyncTests.test_full_sync_retry_reconciles_pending_index_edit_without_duplicate_child \
  scripts.tests.test_orchestrator.OrchestratorTests.test_knowledge_sync_factory_uses_the_started_runs_validated_project_profile -v
```

Result before the factory implementation: three tests passed and the factory test
errored with `AttributeError: 'Orchestrator' object has no attribute
'knowledge_sync'`. After implementation: 4/4 passed.

The explicit lower-intent completion invariant was also observed RED:

```text
python3 -m unittest \
  scripts.tests.test_knowledge_sync.KnowledgeSyncTests.test_phase_never_succeeds_while_a_lower_external_intent_is_pending -v
```

Result before the final-receipt guard: one expected failure because the phase
incorrectly returned `ok=True`. After the guard: 1/1 passed. The guard was retained
only at the final reconciliation boundary so pending create, publish, edit, and
comment intents can first be reconciled by their owning adapters.

End-to-end real-`KuClient` tests use only `FakeTransport` and prove recovery after an
unknown child create, unknown child publish, and unknown index edit. Each retry
finishes with no lower pending intents and exactly one child `create-doc`; child
publish is not repeated after its unknown result is reconciled remotely.

### Pre-Report Verification

```text
python3 -m py_compile \
  scripts/state_store.py scripts/cli_transport.py scripts/clients/icafe_client.py \
  scripts/clients/ku_client.py scripts/knowledge_sync.py scripts/orchestrator.py \
  scripts/tests/test_state_and_artifacts.py scripts/tests/test_adapters.py \
  scripts/tests/test_knowledge_sync.py scripts/tests/test_orchestrator.py

python3 -m unittest \
  scripts.tests.test_state_and_artifacts scripts.tests.test_adapters \
  scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator -v
```

Result: compilation passed and 92/92 focused tests passed in 2.261 seconds.

Production-source scans found no shell execution, physical delete/card creation,
`--no-check-status`, or Codex support in the changed Task 4 paths. All external
adapter coverage used fake transports. No live iCafe/KU write and no BGW/XFlow
build, test, repository, or worktree command was executed.

### Fresh Post-Report Verification

```text
python3 -m unittest \
  scripts.tests.test_state_and_artifacts scripts.tests.test_adapters \
  scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator -v

python3 -m unittest discover -s scripts/tests -v
```

Result: 92/92 focused tests passed in 2.439 seconds, followed by 170/170 full-suite
tests passed in 11.439 seconds. The compile command above also passed again in the
same fresh verification run.

## Fix Round 2 (2026-08-11)

This round addresses the remaining I1 and I5 findings and new N1 finding in
`task-4-rereview.md`.

### Finding Resolution

- **I1 close transaction:** `CafeClient.close()` now owns a durable close-level
  intent keyed by card, target status, the approved canonical input hash, and hashes
  of the reason, KU URL, and exact comment content. The intent stores the original
  expected status without storing raw reason/comment text. A completed close receipt
  replays with zero remote calls. A pending close uses the stored original status:
  current target reconciles the status receipt, current original safely executes the
  same status operation, and any third state fails closed. The exact-content comment
  is then reconciled before the close receipt is committed.
- **I1 canonical KU link:** close approval accepts only canonical HTTPS URLs at exact
  host `ku.baidu-int.com` with path
  `/knowledge/space/category/<repo_id>/<doc_id>`. Userinfo, ports, other hosts,
  malformed paths, queries, and fragments are rejected before approval lookup or
  transport calls.
- **I5 exact started profile:** `Orchestrator.knowledge_sync()` requires the recorded
  path to remain the configured path for the recorded project, recomputes the profile
  bytes hash and compares it with the INTAKE `profile_hash`, reloads validation, and
  requires loaded `project_id` to equal the recorded project before constructing
  adapters.
- **N1 run ownership:** `KnowledgeSync` now requires and stores its construction-time
  run ID. `publish_phase()` rejects a different ID before artifact validation, root
  resolution, durable intent creation, or transport use, and all internal state/root/
  comment identities use the bound ID.

### TDD Evidence

The initial close RED command covered canonical URL validation, completed replay,
status-applied/comment-unknown retry, and third-state handling. Result: 4/4 expected
failures. The old implementation returned the coarse URL reason, made a new snapshot
on completed replay, could not finish the partial retry, and attempted another status
query for the third state.

The profile/run-ownership RED command covered changed profile bytes, mismatched
project ID, and a different publish run ID. Result: both factory tests constructed a
`KnowledgeSync` instead of returning a failure, and the run mismatch reached the KU
transport instead of returning locally.

Focused GREEN cases additionally prove:

- completed close replay returns the exact stored response with no new calls;
- status-applied/comment-unknown recovery uses one status update and one comment
  create, then leaves no pending intents;
- a status-unknown close whose card remains at the original status safely retries the
  same status operation and completes;
- the pending close intent records `expected_current` and hashes only;
- a third card state returns `ICAFE_STATUS_CHANGED` without another write;
- invalid KU URLs, changed profile bytes, wrong project ID, wrong recorded profile
  path, and mismatched run IDs all fail before adapter/transport activity.

### Pre-Report Verification

```text
python3 -m unittest \
  scripts.tests.test_state_and_artifacts scripts.tests.test_adapters \
  scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator -v

python3 -m unittest discover -s scripts/tests -v
```

Result: 98/98 focused affected tests passed, followed by 178/178 full-suite tests in
12.330 seconds. Compilation of all changed production and test modules passed.
Production-source scans found no shell execution, physical delete/card creation,
`--no-check-status`, or Codex support. All adapter tests used fake transports; no
live iCafe/KU write or BGW/XFlow command was executed.

### Fresh Post-Report Verification

```text
python3 -m unittest \
  scripts.tests.test_state_and_artifacts scripts.tests.test_adapters \
  scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator -v

python3 -m unittest discover -s scripts/tests -v
```

Result: 100/100 focused affected tests passed in 2.793 seconds, followed by 178/178
full-suite tests in 11.877 seconds. The changed production and test modules also
passed a fresh `py_compile` run.

## Fix Round 3 (2026-08-11)

This round addresses I1 and N2 from `task-4-rereview-round2.md`.

### Finding Resolution

- **I1 production KU URLs:** `_canonical_knowledge_url()` now accepts exactly four
  variable safe identifiers after `/knowledge/`: `spaceGuid`, `categoryGuid`,
  `repoId`, and `docId`. It still requires HTTPS, exact host
  `ku.baidu-int.com`, and rejects userinfo, ports, query strings, fragments,
  missing/extra path components, empty identifiers, and unsafe identifier bytes.
- **N2 unknown status replay:** a pending close whose matching status write remains
  unknown now returns `QUERY_REQUIRED` while the remote snapshot remains at the
  original status. It does not repeat reachability queries or `card update`, and
  both the close and status intents remain pending. Evidence that the remote card
  reached the target still reconciles the pending status receipt and continues.

### TDD Evidence

The I1 RED case ran the close approval-boundary test with a literal production URL.
It failed because the old validator returned `ICAFE_KNOWLEDGE_URL_INVALID` instead
of reaching the expected `ICAFE_APPROVAL_REQUIRED` branch. After widening only the
four identifier components, that case and the expanded invalid-URL table passed.

The N2 RED case retried a close after an unknown `card update` while the retry
snapshot remained `New`. It failed with two `card update` calls versus the required
one. After adding the pending-status guards, the retry returns `QUERY_REQUIRED`,
keeps exactly the matching close/status intents pending, and performs one total
update. A preservation RED then showed that querying the raw intent table could
misclassify an already receipted status write as unknown; using the pending-intent
view restored completed-receipt recovery. Target-status reconciliation and
third-status rejection also passed.

All tests use fake transports. No live iCafe/KU write, BGW/XFlow build/test,
repository/workspace command, or Codex support action was performed.

### Pre-Report Verification

```text
python3 -m unittest \
  scripts.tests.test_state_and_artifacts scripts.tests.test_adapters \
  scripts.tests.test_knowledge_sync scripts.tests.test_orchestrator -v

python3 -m unittest discover -s scripts/tests -v

python3 -m py_compile \
  scripts/clients/icafe_client.py scripts/tests/test_adapters.py
```

Result: 101/101 focused affected tests passed in 2.515 seconds, 179/179 full-suite
tests passed in 11.739 seconds, and compilation passed.
