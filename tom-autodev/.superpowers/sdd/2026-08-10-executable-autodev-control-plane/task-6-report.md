# Task 6 Implementation Report

## Outcome

Task 6 is complete. The iCode and iPipe runtime implementations are self-contained under `tom-autodev`, reuse `StateStore` and `ApprovalLedger`, and perform external operations only through injected argv/API boundaries. No live iCode/iPipe call, business workspace operation, project build/test, or release command was run.

The workspace is not a Git repository. No repository was initialized and no commit was created. Tests used only temporary Git repositories for read-only iCode worktree/baseline validation.

## RED Evidence

The first run, before runtime modules existed, was:

```bash
python3 -m unittest scripts/tests/test_icode_runtime.py scripts/tests/test_ipipe_runtime.py -v
```

It produced two expected import errors: `clients.icode_runtime` did not exist and `IpipeHttpTransport` / `IpipeTransportError` were absent. Minimal interface stubs were then added so the suite could reach behavior assertions rather than stop at imports.

The behavior-level RED rerun reported `18` tests with `17` expected assertion failures and one `NOT_IMPLEMENTED` transport error. The failures covered missing preflight gates, approval/revision binding, reconciliation, no-replay ownership, discovery, trigger, monitoring, rerun, release, and HTTP retry behavior.

Additional test-first cycles were recorded before their production edits:

- Adapter/orchestrator RED: `4` errors for absent runtime-backed adapters, runtime factories, and collaboration-category routing.
- Credential-evidence RED: `2` failures proving a credential-query CR URL was receipted and authorization text escaped in failure evidence.
- Restart/security RED: `4` cases with `3` failures and `1` error proving build ownership was memory-only, pending rerun did not query/reconcile, secret allowlisted parameters reached approval, and untrusted diagnostics leaked a split token.
- Normalized job evidence RED: `1` failure proving failed-job evidence omitted `ipipe:job/job-1` after raw job payloads were removed.

Every RED failed on the named production behavior. Fake argv/API/HTTP transports and temporary SQLite/Git paths were used throughout.

## Extraction Provenance

Only focused source regions of `/Users/tom/Desktop/skills/tom-autorelease/scripts/autorelease.py` were inspected:

- Lines 334-344: token source priority and `Bearer-` normalization.
- Lines 394-579: bounded iPipe HTTP shape and read/write endpoints.
- Lines 601-839: stage-first normalization, exact build lookup, manual waits, and release association.
- Lines 966-1100: bounded failure/job/stage evidence and rerun candidates.
- Lines 1855-1938: real iCode CLI discovery, top-level command checks, login, and NEW CR query/reconciliation.
- Lines 2018-2086: the real `icode git push_cr --branch ...` submission shape.

The implementation did not copy or retain the old parser, interactive approvals, notifications, lock/state directory, local commit/amend helpers, or runtime path references. Production imports contain only the Python standard library and local `tom-autodev` modules; there is no import, execution, or resolution of `tom-autorelease`.

## Implemented Contracts

### iCode

- `IcodeRuntime.preflight()` validates the exact run-owned registered Git worktree and recorded baseline with read-only Git queries; checks the system Skill directory; resolves only the configured real executable candidates; verifies top-level `api`, `git`, and `login`; and performs login through the injected argv transport.
- `submit()` requires a reviewed, exact revision set and a ledger-backed, run/input-bound G7 approval. It claims a durable `icode.submit` intent before CR query/write, reconciles exact NEW CRs, rejects card/revision/owner/branch conflicts, invokes only `git push_cr`, and never replays an unknown write.
- Receipts contain only normalized run/change/revision/module/branch/commit/CR/patchset/URL fields and canonical evidence refs. Malformed or credential-bearing CR URLs are rejected before persistence.

### iPipe

- `IpipeHttpTransport` reads `COMATE_AUTH_TOKEN` then `~/.comate/login`, normalizes `Bearer-`, sends only `x-ac-Authorization`, bounds timeout/body/retries, retries only transient GETs, never retries POSTs, and sanitizes diagnostics before truncation.
- `IpipeApiClient` exposes pipeline by ID/name/module, revision/recent builds, build by ID, stage/job/detail, release, trigger-by-revision, and manual-stage APIs.
- `discover()` accepts exactly one candidate matching the validated pipeline ID, module, primary trigger revision, full business/test revision map, and parameters. Zero and multiple candidates fail closed.
- `trigger()` rejects non-allowlisted or credential-bearing parameters before approval, requires exact run-bound G8 input, claims intent before the call, verifies the returned build, and reconciles unknown/restarted writes through queries only.
- Durable build bindings allow `monitor()` and `verify_release()` to restore ownership after restart. Monitoring checks stages before aggregate state, bounds/redacts normalized job/log evidence, distinguishes failure/manual wait/timeout/transient/revision mismatch/success, and records environment/failure signatures.
- `rerun()` accepts only a verified failed/manual stage and exact G8 hash, enforces a one-write durable per-stage budget, reconciles pending unknown results by querying the owned build/stage, and never automatically reexecutes on restart.
- `verify_release()` requires exact module, branch, complete revision set, build ID, release rule, and success status; associated drift returns `REVISION_MISMATCH` and no candidate guessing occurs.

### Adapters and Orchestration

- `IcodeClient` and `IpipeClient` expose the complete runtime methods while preserving earlier callable-backed APIs.
- `Orchestrator` creates runtimes with its existing shared StateStore/ApprovalLedger and rejects unknown runs.
- External runtime failures select the existing `DIAGNOSE` transition and attach Task 5 collaboration categories: code/interface/revision to development; tests/environment to test; mixed to both; platform/auth/release rule to project owner. Existing special routes such as `ENV_UNSATISFIED -> ENVIRONMENT_BLOCKED` remain unchanged.

## Files

- Created `scripts/clients/icode_runtime.py`.
- Created `scripts/clients/ipipe_runtime.py`.
- Updated `scripts/clients/icode_client.py`.
- Updated `scripts/clients/ipipe_client.py`.
- Updated `scripts/orchestrator.py` only for runtime factories/failure routing.
- Created `scripts/tests/test_icode_runtime.py`.
- Created `scripts/tests/test_ipipe_runtime.py`.
- Created `scripts/tests/test_runtime_contracts.py`.

No production file outside the Task 6 file boundary was changed.

## Verification

Focused Task 6 plus affected adapter/orchestrator suites:

```bash
python3 -m unittest scripts/tests/test_icode_runtime.py scripts/tests/test_ipipe_runtime.py scripts/tests/test_runtime_contracts.py scripts/tests/test_adapters.py scripts/tests/test_orchestrator.py -q
```

Result: `100 tests in 3.792s`, all passed.

Full discovery:

```bash
python3 -m unittest discover -s scripts/tests -q
```

Result: `247 tests in 13.320s`, all passed.

Compilation:

```bash
python3 -m compileall -q scripts
python3 -m py_compile scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Both commands exited `0` with no output.

Safety/dependency scan:

```bash
rg -n -i 'tom-autorelease|/tmp/tom-autorelease|node_modules|git[[:space:]]+push|\b(?:make|cmake|ctest|ninja|docker|ncs)\b|bgw|xflow' \
  scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py \
  scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Result: no matches (`rg` exit `1`, expected). This proves no runtime dependency, raw push spelling, copied state path, local project build/test/release command, or BGW/XFlow project reference in Task 6 production files.

Process invocation scan:

```bash
rg -n 'subprocess\.(run|Popen|check_call|check_output)|os\.system|shell=True' \
  scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py \
  scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Result: one match at `icode_runtime.py:280`, the read-only `git -C <repo> rev-parse ...` helper used by preflight. There are no shell invocations and no local compile/test/release subprocesses.

Credential-key scans match only deliberate boundary handling: token loading/header construction inside `IpipeHttpTransport`, URL password rejection, and redaction patterns. Tokens/headers are private transport state and are absent from intent payloads, receipts, runtime result schemas, and exception text/diagnostics. Tests additionally inspect SQLite bytes to prove a malicious CR credential is not persisted.

## SHA-256

```text
8c047b7961e1f8d9f005b05dcfda05cc22888c27a5bf0a7e37674fdc6df143d3  scripts/clients/icode_runtime.py
7a5fa0676300687a9af50a8798729b091adfc845a8839d1366490b61f1e086ac  scripts/clients/ipipe_runtime.py
9989c2622370ec2f6584d90d4ce419042beff52ae3df23796d921001a0d8fc07  scripts/clients/icode_client.py
4d2aeff2a924c21da865faaa9d25d0bc2e22ce69a611b8cff9a18ee2f8f58cfa  scripts/clients/ipipe_client.py
7c5c74104e3cbc7f434181fbfb9257ad7217b8dcc74f53cb7493f5b628e98abe  scripts/orchestrator.py
bf5526ecf0b3bbaf647aa176b7778908d448d96b423d8a8ccfefa200e8f7d3a6  scripts/tests/test_icode_runtime.py
8dffef46f37cbd74be87c1ae6eee0699849375a3fc4bbf893ee411109c3fe23b  scripts/tests/test_ipipe_runtime.py
6489d8d06672d686b7e456225fc1ef95b425984195a62e63f858111bc5e02369  scripts/tests/test_runtime_contracts.py
```

## Self-Review

- Mutation checks are covered for wrong gate/run/hash, changed revision/branch/owner/card, ambiguous/empty discovery, forbidden/secret parameters, aggregate success with failed stage, malformed identity, deadline expiry, release association drift, and missing/changed receipts.
- Every external write claims `StateStore` ownership before the call. Concurrent non-owners query only; completed receipts replay; unknown writes stay pending; rerun uses a fixed per-stage key as its durable retry budget.
- External payloads are normalized before returning or persisting. Raw CR/build/stage/job/release responses are not stored. Authorization is confined to the HTTP transport and error/log evidence is bounded and redacted.
- Mac-side subprocess use is limited to read-only Git preflight. The iCode write remains an injected real `push_cr` argv call; iPipe writes remain injected API calls.
- G9 remains enforced by the existing orchestrator evidence/approval policy when exact release evidence advances the release workflow; `verify_release()` itself is read-only and therefore does not consume an approval or perform a release action.

No known Task 6 contract gap remains.

## Task 6 fix round 1

### Review findings and TDD evidence

All 15 independent review findings were rechecked against the implementation and confirmed before editing. The fix was developed in focused RED/GREEN slices using fake argv/API/HTTP transports and temporary SQLite/Git repositories/worktrees only.

- Legacy bypass, fail-closed trigger discovery, and typed discovery RED: `3` selected tests produced `5` expected assertion failures. GREEN: `3/3` tests passed.
- Durable iCode worktree, canonical reviewed Change Set, and complete CR receipt RED: `4` selected tests produced `3` expected failures and `1` expected missing-field error. GREEN: the focused iCode runtime suite passed `12/12` tests.
- Schema-valid pinned iPipe profile RED: `2` expected failures. GREEN: `2/2` tests passed.
- Build/stage/release identity and typed monitor/release errors RED: `3` expected failures. GREEN: `3/3` tests passed.
- Complete rerun G8 binding and replay identity RED: `2` expected failures. GREEN: `4/4` rerun/restart tests passed.
- Orchestrator profile pinning and shared durable dependencies RED: `2` expected construction errors. GREEN: `2/2` tests passed.
- Bounded socket reads RED: `1` expected unbounded-read error. GREEN: all `3/3` HTTP transport tests passed.
- Assistant-compatible user request shapes RED: `1` expected constructor error. GREEN: `1/1` request-shape test passed.
- Emitted runtime failure routing RED: `3` expected failures. GREEN: `3/3` routing tests passed.
- Definite typed write/confirmation errors RED: `2` expected failures. GREEN: `2/2` tests passed.
- Exact-build, failure-evidence, and pending-rerun typed reads RED: `1` expected error and `1` expected failure. GREEN: `2/2` tests passed.

The first affected-suite run after tightening the contracts reported `122` tests with `6` failures and `5` errors. All 11 were stale test fixtures preserving superseded behavior: callable write adapters, profile parameters in the schema-invalid location, unpinned restart runtimes, and a runtime factory test without INTAKE profile evidence. Those tests were updated to the reviewed runtime-only/pinned-profile contracts. The next affected-suite run passed `122/122` tests. After the final typed-read cases were added, the final affected-suite run passed `124/124` tests.

### Changed behavior

- Callable-backed iCode/iPipe adapters are rejected; all writes require runtime gates.
- Trigger discovery fails closed on every non-confirmed result and preserves typed transport reasons.
- iCode submission requires durable `WorkspaceManager` ownership and a canonical hash of the persisted reviewed Change Set. Receipts carry the complete business/test revision set and stronger CR identity validation.
- Every iPipe operation requires the run-pinned, schema-valid profile and byte hash recorded at INTAKE. Orchestrator runtime factories reject pinned-dependency overrides and share `ArtifactStore`/`WorkspaceManager` instances.
- Monitor and release verification require exact build identity, a nonempty set of unique stage IDs, and current aggregate/stage success.
- Rerun G8 hashes bind pipeline, module, environment, build, stage, revision set, complete repositories, and failure signature. Pending and completed replay require the exact ledger-backed approval ID/hash.
- Real HTTP response/error streams are read with `body_limit + 1`; oversized bodies fail as `IPIPE_RESPONSE_TOO_LARGE` before decode.
- `IpipeApiClient` requires a current user and emits assistant-compatible user fields for trigger, manual stage, and stage/job/detail reads.
- Runtime reason families route to `DIAGNOSE`; pipeline evidence classification drives Task 5 collaboration ownership, with login/auth/permission and release/platform cases routed to project-owner categories.
- Typed transport/business reasons are preserved across discovery, monitoring, release, trigger/rerun writes, confirmation queries, failure-evidence reads, and pending-rerun reconciliation. iPipe POST remains single-attempt, and unknown writes remain query-only/no-replay.

### Files

- Updated `scripts/clients/icode_runtime.py`.
- Updated `scripts/clients/ipipe_runtime.py`.
- Updated `scripts/clients/icode_client.py`.
- Updated `scripts/clients/ipipe_client.py`.
- Updated `scripts/orchestrator.py`.
- Updated `scripts/tests/test_icode_runtime.py`.
- Updated `scripts/tests/test_ipipe_runtime.py`.
- Updated `scripts/tests/test_runtime_contracts.py`.
- Updated `scripts/tests/test_adapters.py`.
- Updated `scripts/tests/test_orchestrator.py`.

The workspace is not a Git repository. No repository was initialized and no commit identifier was fabricated.

### Final verification

```bash
python3 -m unittest scripts/tests/test_icode_runtime.py scripts/tests/test_ipipe_runtime.py scripts/tests/test_runtime_contracts.py scripts/tests/test_adapters.py scripts/tests/test_orchestrator.py -q
```

Result: `124 tests in 5.581s`, all passed.

```bash
python3 -m unittest discover -s scripts/tests -q
```

Result: `271 tests in 15.138s`, all passed.

```bash
python3 -m compileall -q scripts
python3 -m py_compile scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Both compilation commands exited `0` with no output.

```bash
rg -n -i 'tom-autorelease|/tmp/tom-autorelease|node_modules|git[[:space:]]+push|\b(?:make|cmake|ctest|ninja|docker|ncs)\b|bgw|xflow' \
  scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py \
  scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Result: no matches (`rg` exit `1`, expected). There is no runtime dependency on the source Skill, raw Git push, copied state path, local project build/test/release command, or BGW/XFlow project reference in Task 6 production files.

```bash
rg -n 'subprocess\.(run|Popen|check_call|check_output)|os\.system|shell=True' \
  scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py \
  scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Result: one match at `scripts/clients/icode_runtime.py:341`, the read-only Git inspection helper. There are no shell invocations or local compile/test/release subprocesses.

### SHA-256

```text
c37a2228aa19e9baf1527640b94db6b5b187a94628ecaad5d13712ed0651e3b6  scripts/clients/icode_runtime.py
8e4b957869d7e7cfe40fa8898ebef2129c8de5d7884264113e12e46fd6ee54ff  scripts/clients/ipipe_runtime.py
a97f08fcc82e877a0a80f4de89744c7833566f2525e7730fa3d7ede84f90e478  scripts/clients/icode_client.py
e6a428a3c4761effeeeadc20e93b06a25cf845ce8d11afc6c393e36d750bf657  scripts/clients/ipipe_client.py
96325ffde419679ee93538bcc0aa6b9560a07a61f1ce118f963ae10df8960d37  scripts/orchestrator.py
cc793419bf6376d438206474256b577cd108121a3df506a772c5e44e1c6f318d  scripts/tests/test_icode_runtime.py
434aff1cfab99085304c43dd485fbe5cdc0f4855dda30abe9e01b056ccba2603  scripts/tests/test_ipipe_runtime.py
d3c3193588d3162e079d1d199f47f67d491af42b94a9fd53d81cbdd49acf48a4  scripts/tests/test_runtime_contracts.py
10774d43b1f062c63b42289fbd0f0fd5c86ab927e096b9c6131419c0b2c4024a  scripts/tests/test_adapters.py
29a8334df9659b977fc9920265fdee2ec9e3339b2bb6412f5cb6c6dfcdc0bcdb  scripts/tests/test_orchestrator.py
```

No live iCode/iPipe call or local BGW/XFlow compile, test, simulator, container, or release command was performed. Live external payload tolerance remains `UNVERIFIABLE_LIVE`, as required by the task constraints.

## Task 6 fix round 2

### Scope and finding verification

This round is scoped only to re-review finding `NEW-I1`. The finding was confirmed before production edits: `IcodeRuntime.preflight()` called `WorkspaceManager.reconcile()`, whose recovery branches can promote `RESERVED` ownership, delete registered/unregistered worktree paths for `FAILED`/`CLEANING`, and update durable ownership state.

The original 15 round-1 findings remain addressed. No live iCode/iPipe operation, local project build/test/release command, or external write was performed.

### RED evidence

The focused regression command was run before production changes:

```bash
python3 -m unittest \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_active_ownership_is_query_only_and_never_calls_reconcile \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_reserved_ownership_fails_closed_without_mutation \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_failed_ownership_fails_closed_without_mutation \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_cleaning_ownership_fails_closed_without_mutation -v
```

Result: `4` tests, `4` expected failures.

- `ACTIVE`: patching `reconcile()` to raise caused preflight to return `WORKTREE_OWNERSHIP_QUERY_FAILED`, proving preflight called recovery rather than a query-only boundary.
- `RESERVED`: preflight promoted durable ownership to `ACTIVE`, changed ownership database bytes, and incorrectly returned `OK`.
- `FAILED`: preflight removed the registered worktree/path and returned `WORKTREE_NOT_OWNED`.
- `CLEANING`: preflight removed the registered worktree/path, changed ownership database bytes, and returned `WORKTREE_NOT_OWNED`.

A second RED run placed mutation snapshots before reason-code assertions for the three recovery states. Result: `3` tests, `3` expected failures; the diffs explicitly showed ownership-byte changes for `RESERVED`/`CLEANING` and path/registration removal for `FAILED`.

Each regression snapshots the durable ownership database bytes, source/worktree path existence, Git worktree registration, and source/worktree porcelain status before and after preflight.

### Changed behavior

- Added public `WorkspaceManager.query_ownership()`. It opens the existing ownership database with SQLite `mode=ro`, validates exact repository/run/task/owner identity and requires durable `ACTIVE` status, then performs only Git worktree registration and `rev-parse` checks.
- `RESERVED`, `FAILED`, and `CLEANING` ownership return `WORKTREE_NOT_ACTIVE` without recovery, cleanup, state transition, path removal, or repository/worktree alteration.
- Exact `ACTIVE` ownership returns `VERIFIED` only when the registered worktree, common Git repository, path, and recorded baseline revision all match.
- `IcodeRuntime.preflight()` now calls only `query_ownership()` for durable ownership. It never calls `WorkspaceManager.reconcile()` or a recovery/cleanup path.

### Files

- Updated `scripts/workspace_manager.py`.
- Updated `scripts/clients/icode_runtime.py`.
- Updated `scripts/tests/test_icode_runtime.py`.
- Updated this report.

The workspace is not a Git repository. No repository was initialized and no commit identifier was fabricated.

### GREEN and final verification

Focused NEW-I1 regressions:

```bash
python3 -m unittest \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_active_ownership_is_query_only_and_never_calls_reconcile \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_reserved_ownership_fails_closed_without_mutation \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_failed_ownership_fails_closed_without_mutation \
  scripts.tests.test_icode_runtime.IcodeRuntimeTests.test_preflight_cleaning_ownership_fails_closed_without_mutation -v
```

Result: `4 tests in 0.792s`, all passed.

Focused iCode and workspace suites:

```bash
python3 -m unittest scripts/tests/test_icode_runtime.py scripts/tests/test_workspace_recovery.py -q
```

Result: `45 tests in 11.231s`, all passed.

Affected Task 6 suites:

```bash
python3 -m unittest scripts/tests/test_icode_runtime.py scripts/tests/test_ipipe_runtime.py scripts/tests/test_runtime_contracts.py scripts/tests/test_adapters.py scripts/tests/test_orchestrator.py -q
```

Result: `128 tests in 6.181s`, all passed.

Full control-plane suite:

```bash
python3 -m unittest discover -s scripts/tests -q
```

Result: `275 tests in 16.480s`, all passed.

Compilation:

```bash
python3 -m compileall -q scripts
python3 -m py_compile scripts/workspace_manager.py scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Both commands exited `0` with no output.

Safety/dependency scan:

```bash
rg -n -i 'tom-autorelease|/tmp/tom-autorelease|node_modules|git[[:space:]]+push|\b(?:make|cmake|ctest|ninja|docker|ncs)\b|bgw|xflow' \
  scripts/workspace_manager.py scripts/clients/icode_runtime.py scripts/clients/ipipe_runtime.py \
  scripts/clients/icode_client.py scripts/clients/ipipe_client.py scripts/orchestrator.py
```

Result: no matches (`rg` exit `1`, expected).

Query-only source scans:

```bash
sed -n '77,167p' scripts/workspace_manager.py | \
  rg -n 'UPDATE|DELETE|INSERT|rmtree|_git_result|reconcile\(|worktree", "remove|_change_ownership_status|_remove_empty_parents'
sed -n '45,100p' scripts/clients/icode_runtime.py | \
  rg -n 'reconcile\(|remove|rmtree|_git_result|_change_ownership_status|_remove_empty_parents'
```

Both scans returned no matches (`rg` exit `1`, expected). The `preflight()` call-site audit shows its durable ownership call is `query_ownership()`; recovery `reconcile()` remains confined to WorkspaceManager create/remove recovery flows.

### SHA-256

```text
397eb07931488970145c34cebaa3c328028fe8cb7bc81bb8cd8dd9a2494f5f35  scripts/workspace_manager.py
22b3624c7f02f6f9f7fc4ebf5c8847a41020f96dea33fa009805e5589f670d07  scripts/clients/icode_runtime.py
8e4b957869d7e7cfe40fa8898ebef2129c8de5d7884264113e12e46fd6ee54ff  scripts/clients/ipipe_runtime.py
a97f08fcc82e877a0a80f4de89744c7833566f2525e7730fa3d7ede84f90e478  scripts/clients/icode_client.py
e6a428a3c4761effeeeadc20e93b06a25cf845ce8d11afc6c393e36d750bf657  scripts/clients/ipipe_client.py
96325ffde419679ee93538bcc0aa6b9560a07a61f1ce118f963ae10df8960d37  scripts/orchestrator.py
712343ac17478b90935b9f1fb32645c06000a5f1e40693c0cf4ae308a64449d0  scripts/tests/test_icode_runtime.py
434aff1cfab99085304c43dd485fbe5cdc0f4855dda30abe9e01b056ccba2603  scripts/tests/test_ipipe_runtime.py
d3c3193588d3162e079d1d199f47f67d491af42b94a9fd53d81cbdd49acf48a4  scripts/tests/test_runtime_contracts.py
10774d43b1f062c63b42289fbd0f0fd5c86ab927e096b9c6131419c0b2c4024a  scripts/tests/test_adapters.py
29a8334df9659b977fc9920265fdee2ec9e3339b2bb6412f5cb6c6dfcdc0bcdb  scripts/tests/test_orchestrator.py
```

Concern: live Comate iCode behavior remains `UNVERIFIABLE_LIVE` by task constraint. This round changes only local ownership verification and performs no external operation.
