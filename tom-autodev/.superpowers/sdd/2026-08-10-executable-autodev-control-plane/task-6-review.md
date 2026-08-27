# Task 6 Independent Review

## Specification Axis Findings

### Critical

#### SPEC-C1 - Legacy adapters still expose external writes outside every Task 6 safety gate

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_client.py:25`, `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:223`, `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:252`
- Evidence: the callable-backed iCode path invokes `submitter()` after comparing two caller-supplied strings, without a ledger-backed G7 record, run-owned StateStore intent, reconciliation, or concurrency ownership. The callable-backed iPipe trigger invokes `transport("trigger", ...)` without G8 or an intent and silently drops non-allowlisted keys; its rerun path accepts only `approved=True` and invokes `transport("rerun", ...)` without G8, durable budget, or recovery semantics. `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_adapters.py:1391` and `:1402` preserve these unsafe paths as supported behavior.
- Concrete impact: callers can bypass run binding, exact approval hashes, intent-before-call, one-writer ownership, and unknown-result no-replay for all three real write classes.
- Affected criteria: all writes are run-bound G7/G8; intent before call; concurrent owner only; pending unknown query-only; forbidden pipeline keys are rejected, not silently filtered.
- Blocking: yes.

#### SPEC-C2 - Trigger fails open after pipeline/discovery verification errors

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:120`, especially `:124-130`
- Evidence: only `BUILD_AMBIGUOUS` stops the path. `PIPELINE_QUERY_FAILED`, `PIPELINE_IDENTITY_MISMATCH`, `BUILD_QUERY_FAILED`, and other non-OK discovery results fall through to `trigger_by_revision()`.
- Concrete impact: the runtime may POST when it could not establish the configured pipeline identity or determine whether an exact build already exists, creating a wrong or duplicate build.
- Affected criteria: exact pipeline/module/revision binding; query candidates before write; zero/multiple candidates return stable confirmation errors; external writes fail closed.
- Blocking: yes.

#### SPEC-C3 - iCode G7 is bound to a declared hash, not the canonical Change Set

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:177`, especially `:188-192`
- Evidence: `canonical_input_hash` is optional and defaults to the same caller-supplied `input_hash`; no canonical hash is computed from immutable Change Set/revision content and no persisted reviewed artifact is resolved. `reviewed=True` and `reviewed_revision_set == revision_set` are also self-asserted by the same input object. The test fixture at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:101` intentionally omits a canonical hash and uses the non-hash string `submit-hash`.
- Concrete impact: a changed repository, target branch, card, owner, or test revision can reuse an approval for unchanged declared hash text and reach `push_cr`.
- Affected criteria: reviewed Change Set only; canonical input hash matches run-bound G7; exact reviewed business/test revisions; approval input changes stop before external call.
- Blocking: yes.

#### SPEC-C4 - Worktree ownership is caller-asserted rather than verified from durable workspace ownership

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:47`, `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:382`
- Evidence: `preflight()` trusts the injected `worktree_bindings` dictionary and verifies only that the supplied path is a Git top level at the declared HEAD. It never checks the durable `WorkspaceManager` ownership record or `git worktree list`. The orchestrator forwards arbitrary caller options. The positive test at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:61` uses the primary `git init` checkout, not a run-created registered worktree.
- Concrete impact: an arbitrary checkout can be labeled with the current run ID and submitted from outside the run-owned workspace.
- Affected criteria: registered Git worktree at recorded baseline; worktree belongs to the run; stale/cross-run submissions fail closed.
- Blocking: yes.

#### SPEC-C5 - Release verification does not require the owned pipeline build to have succeeded

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:338`, especially `:347-371`
- Evidence: `_matches_build()` checks identity, revisions, and parameters but not aggregate or stage status. `verify_release()` can therefore return `OK` for a RUNNING/FAILED build whenever a matching success-shaped release record is returned. It also does not repeat the stage-first check. Tests cover only a successful current build (`/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:326` and `:340`).
- Concrete impact: failed or incomplete pipeline evidence can be accepted as exact release evidence and supplied to the G9 transition.
- Affected criteria: release verification occurs after pipeline success; release requires exact build/revisions/branch/rule/status; failed stage cannot be hidden by aggregate state.
- Blocking: yes.

### Important

#### SPEC-I1 - Emitted Task 6 failures do not reliably reach Diagnose or the required owner

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:223`, `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:635`, `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:656`
- Evidence: `_is_runtime_failure()` recognizes a short substring list but omits emitted families such as `CR_*`, `SUBMIT_*`, `TRIGGER_*`, `RERUN_*`, `MONITOR_TIMEOUT`, `BUILD_*`, and generic `RELEASE_*`. In an iPipe state many therefore fall through to STOPPED/invalid transition instead of Diagnose. For `PIPELINE_FAILED`, routing ignores `evidence["classification"]`, so a normalized `TEST_FAILURE` or `ENVIRONMENT_FAILURE` is categorized as `mixed`. `ICODE_LOGIN_REQUIRED` is likewise categorized as mixed rather than auth. The contract test at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_runtime_contracts.py:65` tests hand-picked synthetic reason codes rather than the runtime's actual result vocabulary.
- Concrete impact: diagnosis can be skipped or rejected and collaboration can notify both development/test instead of test or project owner.
- Affected criteria: every iCode/iPipe failure routes to Diagnose; code/interface/revision -> development; tests/environment -> test; mixed -> both; platform/auth/release rule -> project owner.
- Blocking: yes.

#### SPEC-I2 - The iCode receipt omits the complete reviewed revision set

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/icode_runtime.py:237`, especially `:243-257`
- Evidence: the persisted response contains only the business `commit_revision` and `revision_set_id`; it contains neither the business/test revision objects nor an evidence reference for the test revision. `_HEX_REVISION` at `:17` is unused, change number is not structurally validated, and the CR URL is only syntactically checked rather than tied to the accepted change number/module.
- Concrete impact: downstream consumers cannot prove that the submitted receipt corresponds to the reviewed test repository revision, and malformed/cross-module identity can be normalized into a durable success receipt.
- Affected criteria: verified receipt contains business/test revisions, module, target branch, CR/patchset/current revision, canonical CR URL, and canonical revision evidence.
- Blocking: yes.

#### SPEC-I3 - Monitoring accepts missing stage identity and can report success with no stages

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:165`, `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:191`, `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:556`
- Evidence: stage normalization permits an empty `stage_build_id`, stores it as owned, and never verifies a returned build ID. `all(stage["status"] in _SUCCESS for stage in [])` is true, so aggregate SUCCESS plus an empty stage response returns pipeline success.
- Concrete impact: an incomplete/malformed stage response can hide stage failure and can create an empty stage rerun identity.
- Affected criteria: stage-first failure evidence; expected stage identity is mandatory; rerun accepts only an exact owned stage; successful response without expected identity is not success.
- Blocking: yes.

#### SPEC-I4 - Rerun approval identity is incomplete and completed replay accepts a changed approval

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:217`, especially `:219-228` and `:243-264`
- Evidence: completed replay compares only `approval.input_hash`, not the persisted `approval_id` or ledger record, although pending replay does compare both. The hash binding excludes pipeline ID, module, and environment fingerprint; those are added to the intent payload only after the approval hash is computed.
- Concrete impact: a different approval object can replay a completed rerun, and G8 does not cryptographically bind all exact pipeline/environment ownership fields required for that stage action.
- Affected criteria: G8 binds exact stage/input; changed stage/build/revision/approval conflicts; exact pipeline/module/revision/environment binding.
- Blocking: yes.

#### SPEC-I5 - The runtime accepts unvalidated, unpinned project profiles

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:490`, `/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:394`
- Evidence: `_context()` performs partial ad hoc shape checks and accepts any environment dictionary; it does not call `validate_profile()`, verify the run's recorded profile hash, or require the full environment schema. The orchestrator passes no recorded profile binding into the runtime.
- Concrete impact: discovery, trigger, build ownership, and release rules can be based on a profile that was never validated or that differs from the run's confirmed profile.
- Affected criteria: discover/trigger use only the validated project profile; exact allowlist/pipeline/environment binding; release uses the configured rule.
- Blocking: yes.

### Minor

No non-blocking Specification findings. The confirmed issues above affect mandatory safety or acceptance behavior.

## Standards Axis Findings

### Critical

No additional Critical Standards finding beyond the Specification safety defects.

### Important

#### STD-I1 - HTTP response/error reads are not actually bounded

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:287`, especially `:291-295`
- Evidence: both success and `HTTPError` paths call `read()` with no byte limit. `body_limit` is applied only after the complete body is allocated, decoded, and sometimes JSON-serialized. The test at `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:357` supplies an already-materialized fake body and therefore verifies diagnostic truncation, not bounded network consumption.
- Concrete impact: a large/malicious iPipe response can consume unbounded memory and processing before any bound or redaction takes effect.
- Affected criteria: bounded response/error bodies; bounded/redacted diagnostics.
- Blocking: yes.

#### STD-I2 - The write/query client is not compatible with the installed iPipe assistant contract

- Classification: `CONFIRMED` for the static interface mismatch; live server tolerance is `UNVERIFIABLE_LIVE`.
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_client.py:180`, `:184`, `:195`, `:199`
- Evidence: `trigger_by_revision()` has no current-user input and omits the assistant contract's `user` query field and `triggerUser` body field. `manual_execute_stage()` has no username input and omits its required `username` query field. Stage/job reads also omit the current-user field used by the source contract. Compare `/Users/tom/.codex/skills/ipipe-pipeline-assistant/scripts/ipipe_client.py:250` and `:334`.
- Concrete impact: real trigger, manual continuation/rerun, stage, and failed-job calls may be rejected or return incomplete identity, even though fake transports pass.
- Affected criteria: injected transport compatible with `ipipe-pipeline-assistant`; required read/write APIs are production-usable; business identity is verified.
- Blocking: yes.

#### STD-I3 - Runtime tests use a profile that the repository validator rejects

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:18`, especially `:25` and `:29`
- Evidence: the fixture adds `pipeline_profile.parameters`, but `/Users/tom/Desktop/skills/tom-autodev/schemas/project-profile.schema.json:42` has `additionalProperties: false` and no `parameters` field. Its environment profile also omits six required fields from schema lines `55-73`. Consequently the allowlist/secret/hash tests never exercise a profile that can come through `load_profile()`.
- Concrete impact: adapter/runtime integration can fail or silently lose its parameter payload despite green unit tests; the tests do not prove validated-profile compatibility.
- Affected criteria: validated profile only; exact allowlisted payload and environment fingerprint; legacy compatibility; acceptance tests assert external behavior.
- Blocking: yes.

#### STD-I4 - Runtime exception handling erases definite transport/business reason codes

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/clients/ipipe_runtime.py:51`, `:61`, `:158`, `:346`
- Evidence: broad `except Exception` blocks convert typed `IpipeTransportError` values such as `AUTH_REQUIRED`, `PERMISSION_DENIED`, `OBJECT_NOT_FOUND`, and definite 4xx business failures into generic query failures or `PIPELINE_TRANSIENT`. The transport has stable reason codes, but the runtime discards them before orchestration.
- Concrete impact: definite failures are misreported as transient/query failures and cannot reach the correct project-owner collaboration route.
- Affected criteria: parse and preserve HTTP/business response status; 4xx other than 429 is definite; stable reason codes; correct failure routing.
- Blocking: yes.

#### STD-I5 - Required negative/integration coverage is missing for the failing boundaries

- Classification: `CONFIRMED`
- Repository: `/Users/tom/Desktop/skills/tom-autodev`
- Location: `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_runtime_contracts.py:46`, `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_ipipe_runtime.py:164`, `/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_icode_runtime.py:122`
- Evidence: there is no test for legacy write-gate bypass, discovery errors before trigger, canonical Change Set recomputation, durable WorkspaceManager ownership, empty/malformed stage identity, failed current build during release verification, changed completed-rerun approval ID, actual emitted failure routing, bounded socket reads, or validated-schema profile compatibility.
- Concrete impact: the reported 100/247 passing tests can remain green while all blocking defects above are present.
- Affected criteria: RED fake-contract coverage for all required safety cases; adapter/orchestrator contract coverage; external behavior assertions.
- Blocking: yes.

### Minor

No separate Minor Standards finding. The smaller validation issues are evidence supporting blocking findings rather than independent style concerns.

## Axis Verdicts

- Specification: `FAIL` - five Critical and five Important `CONFIRMED` blocking findings.
- Standards: `FAIL` - five Important `CONFIRMED` blocking findings.
- Overall: `FAIL`. Task 6 must return to `tom-diagnose`; it is not eligible for G7 iCode approval.

## Confirmed Conformance

- The reviewed production files contain no `tom-autorelease` runtime reference, raw `git push`, local BGW/XFlow build/test/release command, shell execution, or non-Comate approval provider reference.
- iCode production submission spells only `icode git push_cr`; iPipe POST retry count is one; StateStore intent claims serialize the new runtime paths; pending runtime intents are not automatically replayed.
- The checked file hashes match the Task 6 implementation report exactly.

## Unverifiable Live Items

These are evidence limitations, not substitutes for the confirmed findings and do not change the `FAIL` verdict:

- `UNVERIFIABLE_LIVE`: actual Comate iCode help/login output, NEW-CR response fields, URL canonicality, and `push_cr` receipt behavior. No live iCode command was allowed.
- `UNVERIFIABLE_LIVE`: live iPipe envelope, multi-repository revision association, default parameter behavior, user-field tolerance, stage/job identity, and release payload shape. No live iPipe query/write was allowed.
- `UNVERIFIABLE_LIVE`: real remote concurrency/eventual-consistency behavior after unknown writes. Static one-writer/query-only logic was inspected, but no external operation was allowed.
- `UNVERIFIABLE_BY_REVIEW_INSTRUCTION`: the implementation report's recorded test and compilation results were not rerun; this review was explicitly static.

## Review Baseline

- Baseline type: exact non-Git workspace snapshot. `/Users/tom/Desktop/skills/tom-autodev` is not a Git repository, so no commit/diff baseline exists.
- Brief SHA-256: `6c3fc243870876d51e84ab7ca6974788536845b2911f7f11583ce9b253cd59a4`
- Implementation report SHA-256: `f7f37771684e6dfb47695f3fda67a431e7d612a1c8956e852c9004013ed706e6`
- Reviewed input manifest SHA-256: `1c0b839fdfd000631bd037a0bdbd6d1244c24a8cf77099cb5a90ef316608de4a`
- Production/test SHA-256 values: identical to the eight values recorded in `task-6-report.md`.
- Review method: static source/interface/hash inspection only; no tests, compilation, live iCode/iPipe call, project command, or release action was run.
