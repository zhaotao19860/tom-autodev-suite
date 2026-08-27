# Task 6 Fix Round 1 Re-review: iCode, iPipe, and Release Runtime

## Verdict

- Specification: **FAIL**
- Standards: **PASS**
- Overall: **FAIL**
- Original findings: all 15 are **ADDRESSED**
- New findings: one Important Specification blocker (`NEW-I1`)

This was a static re-review of fix round 1. No tests, compilation, live iCode/iPipe operation, local BGW/XFlow command, or release action was run by this reviewer.

## New Important Finding

### NEW-I1 - `IcodeRuntime.preflight()` can mutate ownership state and remove a worktree

- Classification: `CONFIRMED`
- Axis: Specification
- Severity: Important
- Blocking: yes
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Evidence: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:67` calls `WorkspaceManager.reconcile()` from `preflight()`. That operation is not read-only. For a `RESERVED` record it can persist `RESERVED -> ACTIVE` at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:419`, delete an unregistered worktree directory at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:443`, and remove empty parent directories at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:452`. For `FAILED` or `CLEANING`, it can run `git worktree remove --force` at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:474`, delete the worktree directory at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:492`, remove parents at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:510`, and transition `CLEANING -> FAILED` at `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py:512`.
- Concrete impact: a nominal iCode readiness check can change durable workspace ownership or delete a registered/unregistered checkout before it returns a preflight result. That violates the control-plane boundary for a check that is required to inspect, not repair or clean up, the repository.
- Exact affected acceptance criterion: Task 6 iCode contract 2 requires `preflight()` to verify the registered run-owned worktree and states, "It must not amend, commit, push, or alter the repository." The orchestration contract likewise limits the Mac-side path to inspection and reviewed source/test operations; preflight does not authorize workspace recovery or cleanup.
- Affected tests: `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:162` claims read-only behavior but its fixture creates an already `ACTIVE` worktree at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:72` and compares only `git status` at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:163` and `:169`. It does not assert unchanged ownership bytes/status and unchanged path existence for `RESERVED`, `FAILED`, or `CLEANING` records, so every mutating branch above is uncovered.
- Required correction: make preflight use a query-only durable ownership check plus read-only Git inspection. Do not call recovery/cleanup reconciliation. Add regressions that snapshot durable ownership and filesystem/worktree registration before and after preflight for `ACTIVE`, `RESERVED`, `FAILED`, and `CLEANING` ownership states.

## Original Finding Disposition

| Finding | Disposition | Concrete evidence |
| --- | --- | --- |
| `SPEC-C1` | **ADDRESSED** | Callable write backends are rejected at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_client.py:14` and `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:250`; regressions are at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_runtime_contracts.py:47` and `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:1392`. |
| `SPEC-C2` | **ADDRESSED** | `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:126` queries discovery first, returns every result other than `BUILD_QUERY_REQUIRED` at `:137`, and reaches POST only at `:142`; the discovery-error case is covered at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:276`. |
| `SPEC-C3` | **ADDRESSED** | Persisted reviewed artifact content is loaded and compared byte-for-byte, then its SHA-256 and caller hash are recomputed/checked at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:223` through `:249`; negative coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:180` and `:195`. |
| `SPEC-C4` | **ADDRESSED** | Ownership authenticity is now checked against durable `WorkspaceManager` state and exact path/baseline at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:63` through `:78`; the fixture uses a real temporary `git worktree` at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:72`, and a forged primary checkout is rejected at `:174`. The separate mutation caused by choosing `reconcile()` for this check is tracked as `NEW-I1`. |
| `SPEC-C5` | **ADDRESSED** | Release verification re-queries the exact build and requires aggregate success plus a nonempty, uniquely identified, all-successful stage set at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:393` through `:411`; negative coverage begins at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:470`. |
| `SPEC-I1` | **ADDRESSED** | Runtime families now route through `DIAGNOSE` at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:227` through `:275` and `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:683` through `:719`; actual family and evidence-classification cases are covered at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_runtime_contracts.py:95` through `:147`. |
| `SPEC-I2` | **ADDRESSED** | Receipt validation binds numeric change number, patchset, declared module and change-number URL, and the receipt persists the complete revision set and all revision evidence at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:285` through `:319`; coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:224` and `:236`. |
| `SPEC-I3` | **ADDRESSED** | Missing, empty, and duplicate stage identities fail closed at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:192` through `:195` and `:638` through `:644`; coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:360`. |
| `SPEC-I4` | **ADDRESSED** | Rerun binding includes pipeline, module, environment, build, stage, revisions and failure signature at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:247` through `:299`; replay verifies the exact approval ID/hash and ledger record at `:771` through `:787`. Rerun/replay coverage begins at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:386`. |
| `SPEC-I5` | **ADDRESSED** | The production factory rejects dependency overrides and loads the exact INTAKE profile path/byte hash through schema validation at `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:404` through `:453`; runtime requires exact pinned profile equality at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:478` through `:489`. Coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:221` and `:244`. |
| `STD-I1` | **ADDRESSED** | Both success and HTTP-error streams use `read(body_limit + 1)` and reject oversize input before decode at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:306` through `:328`; socket-read-size coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:657`. |
| `STD-I2` | **ADDRESSED** | The API client requires a current user and includes assistant-compatible user fields on stage/job/detail reads, trigger, and manual execution at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:151` through `:238`; exact request-shape coverage starts at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:695`. |
| `STD-I3` | **ADDRESSED** | The iPipe test fixture is schema-valid and puts runtime parameters in the revision set rather than the profile at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:21` through `:60`; pinned validation coverage starts at `:212`. |
| `STD-I4` | **ADDRESSED** | Typed `IpipeTransportError.reason_code` is preserved by the shared helper at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:813` and across discovery, monitoring, release and write confirmation paths, including `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:57`, `:146`, `:183`, and `:393`; negative coverage is at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:501` through `:613`. |
| `STD-I5` | **ADDRESSED** | The formerly absent boundary cases now have negative/integration coverage across `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:162` through `:245`, `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:276` through `:750`, `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_runtime_contracts.py:47` through `:147`, and `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:221` through `:267`. `NEW-I1` identifies the narrower new recovery-state coverage gap. |

## Parent-Directory Invocation Assessment

Classification: `REJECTED_WITH_REASON` as a Task 6 breakage.

The canonical supported full-suite command is `python3 -m unittest discover -s scripts/tests -q` from `/Users/tom/Desktop/skills/tom-autodev`, as recorded at `/Users/tom/Desktop/skills/tom-autodev/.superpowers/sdd/2026-08-10-executable-autodev-control-plane/task-6-report.md:208`. The tests bootstrap imports from their `scripts` parent, for example `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:11` and `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:12`. A differently spelled parent-directory module/path invocation through the hyphenated `tom-autodev` directory is not the declared package or execution interface. Its import failure therefore does not establish a Task 6 regression.

The coordinating session reports a fresh canonical run of `271/271 PASS`, `compileall` exit 0, and a clean safety scan. Those results are supplied evidence only; this static reviewer did not rerun them.

## Axis Assessment

- **Specification: FAIL.** The authenticity, gate, identity, release, routing, and profile corrections address all ten original Specification findings, but `NEW-I1` violates the explicit non-mutating `preflight()` contract and is blocking.
- **Standards: PASS.** All five original Standards findings are addressed. No new Critical or Important Standards defect was found in the fix round. The uncovered recovery-state test cases support `NEW-I1` but are not double-counted as a separate Standards finding.
- **Overall: FAIL.** Task 6 must return to diagnosis for `NEW-I1`; it is not eligible for a passing review while preflight can perform workspace recovery or cleanup.

## Review Basis and Hashes

- Baseline type: exact non-Git workspace snapshot. No commit or diff baseline was fabricated.
- Brief SHA-256: `6c3fc243870876d51e84ab7ca6974788536845b2911f7f11583ce9b253cd59a4`
- Original review SHA-256: `1e36e47a280f1bea2a550ff1bc6bf119a26f26c9e009e5575b043437d82cfb1b`
- Fix report SHA-256: `4cf18796687ad29b287a1eea3f91f4a9b0c312ab7372c98e0307adc7c0dec8d1`
- Ordered review input manifest SHA-256 (brief, original review, fix report, then the ten fix-report files): `9b0caea48af6f93ebd6a5c4b414bbcc419311f84d4b6b17a9d0d5cd84b6f92ed`
- Ordered ten-file fix snapshot SHA-256: `b032b33eb51fa8f3c3397052c3c0d058970f289ad1c8be05e9299dc8bbdf2aae`
- Reviewed dependency `/Users/tom/Desktop/skills/tom-autodev/scripts/workspace_manager.py` SHA-256: `7b02b6bf111f9da148699dcd307a1036bebe8718b91b56022f83a0da422b840b`
- Schema `/Users/tom/Desktop/skills/tom-autodev/schemas/project-profile.schema.json` SHA-256: `5bfa711ea17efabc4d271eb8dd3064ea2f29fcf753a57bfdf2cd5f74f6da8cf7`
- All ten implementation/test hashes match the fix report at `/Users/tom/Desktop/skills/tom-autodev/.superpowers/sdd/2026-08-10-executable-autodev-control-plane/task-6-report.md:239` through `:249`.
- Static forbidden-reference scan found no `tom-autorelease`, raw `git push`, or local build/test/release command in the five Task 6 production files. The `release` match at `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:418` is a release-data loop, not a local release command.

## Unverifiable Items

- `UNVERIFIABLE_BY_REVIEW_INSTRUCTION`: all recorded test and compilation results, including the coordinating session's fresh `271/271` result, because this reviewer was instructed not to execute them.
- `UNVERIFIABLE_LIVE`: real Comate iCode and iPipe response tolerance, remote reconciliation, and eventual-consistency behavior. No live external operation was permitted.

