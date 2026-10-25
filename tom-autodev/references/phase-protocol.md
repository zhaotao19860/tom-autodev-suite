# Phase Protocol

Every phase exchanges one immutable `ArtifactEnvelope` through the parent `tom-autodev` controller. The envelope is schema-versioned and content-addressed:

```text
action_id, source_event_id, host=comate, run_id, phase, task_id,
schema_version, input_hash, content_hash, source_revisions,
parent_artifact_hash, knowledge_doc_id, knowledge_url, knowledge_version,
icafe_comment_id, evidence_refs, approval_id, approval_input_hash, content
```

The child skill must validate the predecessor hash and the phase schema before producing `content`. The parent persists the envelope through `ArtifactStore`, publishes it with `KnowledgeSync` to the configured KU project parent, and adds an iCafe comment containing the artifact URL and hash. A phase is complete only after the KU receipt, iCafe comment receipt, and the approval gate defined for that phase; evidence-only controller prerequisites explicitly have no new approval. Never edit an iCafe requirement body; comments carry phase links and status.

| Phase | Schema | Input | Gate | Required outcome |
|---|---|---|---|---|
| GRILL | `decision-log` | requirement snapshot + G0 | G1 | `CLARIFIED` or `NO_OPEN_DECISIONS`, plus an `acceptance_delta` when the card arrived without acceptance criteria |
| SPEC | `spec` | grill artifact | G2 | behavior, test interface, environment, traceability |
| TASKS | `task-dag` | approved spec | G3 | acyclic end-to-end frontier DAG |
| PLAN | `task-plan` | task + WorkspaceGate | G4 | executable per-task plan |
| IMPLEMENT | `change-set` | approved, hash-pinned plan + owned worktrees | G5 | business/test candidate diff |
| REVIEW | `review` | fixed change set | no phase approval; parent G7 after PASS | independent Standards and Spec verdict |
| DIAGNOSE | `diagnosis` | frozen failure bundle | G6 | one falsifiable cause and repair route |

Child skills return to the parent on missing input, hash mismatch, unavailable provider, or unresolved approval. They never call iCafe, KU, iCode, Infoflow, or iPipe directly. The current Comate Agent executes the child phase after loading its skill; no separate host LLM/Implement runner is required. `resume` only lists uncertain intents and the checkpoint; it neither reconciles them itself nor executes or completes a phase. All compilation, tests, regression, integration, NCS, simulator, Docker, and release execution happens in the approved iPipe runner; a Mac invocation must return `LOCAL_EXECUTION_FORBIDDEN`.

The order is strict: `next -> load child skill -> read pinned inputs -> validate
predecessor/schema/revisions/ownership -> generate artifact -> EvidenceGate -> applicable
human approval -> complete-phase` (REVIEW has no phase approval). A repeated action that has not advanced the checkpoint
must not reissue resume or duplicate approval. IMPLEMENT must obtain G5 APPROVE before
`complete-phase`, and completion transitions to REVIEW.

Read `input_artifacts` by their immutable IDs through the integrity-checked artifact
reader, then read the actual pinned contents, not just their paths or summaries.
IMPLEMENT must use the pinned Plan and verify both business/test worktrees through
`WorkspaceManager.query_ownership` with run/task/owner token and baseline revisions
before any edit. Preserve user changes; a conflict stops execution. The bounded producer
turn uses its Read/apply_patch (or an available Agent delegation), not a new LLM service,
to generate the candidate `DraftContent` and submit it with `submit-draft`.
Save the real candidate and its approval input hash before asking the parent for G5.
On APPROVE the worker re-reads that same candidate, verifies the exact ledger binding, and
owns `complete-phase` for archival and transition — the producer turn never calls
`complete-phase` or `advance` itself.
Hash drift requires a new human decision, while an unchanged action reuses its gate.
Missing remote authorization/receipts remain a block, not permission to fake evidence.

A `PLAN -> IMPLEMENT` event pins the completed Plan's artifact ID and content hash. IMPLEMENT resolves this pin before any latest-by-task lookup; an invalid, cross-run, cross-task, or hash-mismatched pin fails closed as a missing predecessor. `orchestrator.py recover-rebuilt-change-set RUN TASK PLAN_ARTIFACT` is the local-only recovery entry for rebuilding a candidate: it verifies that immutable Plan, records an idempotent `IMPLEMENT` checkpoint with the pin, and performs no worktree, database, or remote adapter operation.

## Parent Controller Phases

The following phases are controller-owned. They use the same envelope shape but do not delegate adapter ownership to child skills.

SUBMIT freezes a `pipeline_plan` from each task's current Review-bound descriptor and exact
`(change_set_id, revision_set_id)` receipt. Conflicting snapshots of one repository require
local Git ancestry proof; missing objects or divergent commits return `REVISION_AMBIGUOUS`.
No live CR query mutates this plan. A module's build identity covers its own repository,
the product-test repository and declared transitive dependencies; omitted `depends_on`
means all repositories. The plan itself also binds the immutable submission references.
Identical build inputs may reuse an owned successful build even after a new receipt;
changed code, tests, dependencies, pipeline, release rule or environment require new evidence.

Change status comes from this run's frozen descriptors/plan, never worktree cleanliness.
`product_inputs` includes non-release test modules and records changed, branch and candidate
revision. Legacy plans resolve only an explicit `modules` or `unchanged` entry; missing status
blocks. An unchanged module has no CR or new release obligation. Its input must be a
successful **BranchPipeline release** job of the target branch, proving the actual published
source revision and a downloadable `releaseOutputHttpUrl`. The published revision must equal
the revision the plan froze for that module; a release of any other revision (for example the
branch's newest one) is not this module's input and is refused as `PRODUCT_REVISION_MISMATCH`.
Changed modules require owned, exact-candidate **ChangePipeline compile** evidence.
The URL alone, an arbitrary compile, latest listing or a hardcoded node is never evidence.

If no branch release product is available, prepare `ipipe.branch_compile` under G8, binding
run/plan/module/branch/revision/pipeline, compile-only scope, release exclusion and the adapter
capability contract. Claim a durable intent before sending; after timeout/crash only query
the exact intent and persist its receipt, never trigger again. The fake adapter exercises this
contract; the real adapter returns `COMPILE_ONLY_TRIGGER_UNSUPPORTED` because neither reviewed
client nor CLI help documents stage selection or a guaranteed stop before release. Do not
invent a skip-release JSON field or change a pipeline template.

Planned IPIPE evidence binds its parent to that module's input hash and must match a durable
runtime build binding. RELEASE's action hash includes the plan hash and every required
IPIPE artifact. `module_releases` records the complete module set and each proof must match
the runtime's persisted platform-verification receipt. Platform release records must name
the build and its complete revision map. A new release write without a frozen plan returns
`PIPELINE_PLAN_REQUIRED`; completed historical receipts remain readable. `workflow-spec-v2`
blocks active v1 runs through the common execution guard rather than reusing old approvals.

| Phase | Required inputs | Output and receipts | Completion predicate | Approval ownership/binding | Stop conditions |
|---|---|---|---|---|---|
| `WORKSPACE` | G3-approved `task-dag`, selected frontier task, project profile, repository ownership and baseline evidence | WorkspaceGate receipt with owned worktree IDs, exact business/test baseline revisions, lock/cleanup evidence, and content hash | Both worktrees are owned, clean against the recorded baseline, and all required repositories/test profile are verified; the business worktree is the repo the task's `business_module` names (a multi-repo run selects by module, not `business_repos[0]`); receipt is a prerequisite for Plan | No new approval: G3 remains approval of the complete Task DAG/frontier; G4 is exclusively parent approval of the completed exact Task Plan hash | `BASELINE_UNVERIFIED`, ownership/lock conflict, missing test repository, `WORKSPACE_TASK_REPO_UNRESOLVED`/`WORKSPACE_TASK_REPO_MISMATCH`, stale graph/profile, or hash drift |
| `SUBMIT` | G7-approved dual-axis Review PASS, fixed Change Set hash, repository revisions, and submission policy | iCode submission receipt containing CR IDs, revision set ID, target branches, business/test revisions, and submission hash | iCode confirms the exact approved revision set once and emits a durable receipt | Parent owns G7 through Comate/Infoflow; approval input hash equals the reviewed Change Set/submission hash | Review non-PASS, stale/mismatched baseline or hash, missing receipt, iCode rejection/unknown result, or duplicate intent |
| `IPIPE` | iCode submission receipt, pinned pipeline/module/release profile, business/test revisions, environment fingerprint, and G7 binding | `ipipe-evidence` artifact with pipeline/build/stage/job IDs, revisions, profile/environment fingerprints, test results, logs/evidence refs, and terminal stage result; every module/build/stage mapping is represented in the remote refs | Every required remote build/unit/regression/integration stage returns a verified result for the exact revisions and profile; success advances to RELEASE | Parent owns G7 for the initial trigger and G8 for failed-stage rerun or manual continuation; each rerun approval binds the failed-stage evidence/input hash | Revision/profile/environment mismatch, missing or unverifiable evidence, `CODE_FAILURE`/`TEST_FAILURE` to DIAGNOSE, environment failure to ENVIRONMENT_BLOCKED, or pipeline transient without bounded retry |
| `RELEASE` | Verified successful iPipe evidence, release rule/version mapping, exact revisions, environment fingerprint, and G9 context | `release-evidence` receipt with release version, package/deployment identity, provenance, approver, and remote release result | Release action is confirmed once and the version/provenance matches iPipe evidence, then state becomes `RELEASE_SUCCESS` | Parent owns G9; approval binds exact release-evidence/input hash and revision/environment fingerprints | Missing/failed release evidence, version/provenance mismatch, environment failure, duplicate/unknown release result, or approval timeout/rejection |
| `RELEASE_SUCCESS` / `STOPPED` | Terminal result event and all preceding durable evidence (or explicit stop reason) | No next phase; final state receipt and immutable run summary | `RELEASE_SUCCESS` requires verified release evidence; `STOPPED` requires an explicit reason and persisted handoff | No new gate; G10 is available only after a terminal summary and is separately parent-owned | Any `next`, submit, rerun, release, or optimization attempt lacking the terminal predicate is rejected as `TERMINAL_STATE` |

### IPIPE Re-build Event

`ipipe-rebuild` (see README) appends one `IPIPE` event whose payload is a deep copy of the IPIPE entry
payload, so `frozen_plan`, the G7 binding and every reader that scans the latest `IPIPE` event keep
working, plus `previous_state` (`DIAGNOSE`, or `IPIPE` for a second re-build) and a `rebuild` record:
`kind: IPIPE_REBUILD`, `attempt`, `approval_id`/`approval_input_hash` (the G8), `original_build_id`,
`build_id`, `pipeline_id`, `module`, `revision_set_id`, `reason`, `event_type`, `adopted`, `from_state`,
`from_event_id`, `superseded_job_ids`. The pipeline plan holds revisions, not build ids, so its hash does
not change; the build the run follows is the one with an `ipipe.build-binding` that has no
`ipipe.build-superseded:{run}:{build}` marker. The marker, the event and the job supersession commit in
one transaction (`commit_transition_result(..., supersede_jobs=)`); old events, receipts, artifacts and
stage bindings are never edited. The G8 binds `operation: ipipe.rebuild`, the original build, the attempt
and the reason; the trigger intent uses `ipipe.trigger:{run}:{pipeline}:{revision_set_id}:attempt-N`
(the key without a suffix is attempt 1).

### G8 Controller Action

G8 is a parent-owned controller action, not a child phase. Its binding includes the exact
stage identity, parameter declarations and redacted resolved values, and selected product
proofs. BGW maps `get_bgw_test_case` to `baidu/nsiqa/x86bgw` and `get_bgwagent` to
`baidu/sysip/bgwagent` according to the frozen product policy above. Empty manualParams
must still inspect full configuration `stages[].paramBeans`, job inputs and upstream
`upstreamStageId` dependencies; shell references without declarations block.
Live configuration is labelled as live, never claimed to be a historical build snapshot.
Only an adapter with a verified exact job override contract can execute task overrides.
The real adapter blocks with `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`; arbitrary POST JSON or
CLI `--param` alone does not establish override precedence. No template modification is allowed.

Read-only platform observations on 2026-10-12:

- CLI 0.2.0 exposes trigger start/run and retry `--param`, but no compile-only flag or job
  scope flag. The Python skill client uses revision trigger and stage build POST, without
  proof of compile-only or job override semantics.
- bgwagent BranchPipeline 348141, target branch `x86bgw-dpdk-2011`, build 216926594,
  release stage 524779301/job 821125396 exposes successful release, source revision
  `97860aca1dc9241abd55db4b38f05670db1afb98`, version 2508.20.1.1 and releaseOutputHttpUrl.
  Authenticated bounded download verified gzip magic through the actual 307 signed redirect.
  The second hop receives no IREPO-TOKEN; the signed URL is not persisted. This product
  passed the read-only download check; it does not prove this run's P0 execution passed.
- The listing `/api/rest/v10/pipeline-build/builds` needs documented `name` and `token`
  parameters present. Omitting them returned OBJECT_NOT_FOUND; the corrected read returned
  the branch candidate. The bounded listing is discovery, not proof of product absence.
- The skill's pipelineConfInfo-by-name endpoint returned OBJECT_NOT_FOUND on this gateway.
  Product discovery uses the verified getPipelineInfoByModule read and exact module/name
  filtering instead; duplicate/missing identities block.
- Ordinary `p0级自动化回归测试` build stage **645275912**, configuration stage **4875670**,
  owns `get_bgwagent` in its stage-level `paramBeans` and `inParamsMap`: `isDynamic=false`,
  `upStageParam=false`, static default node **97110098**. For example, job configuration
  **9638665** / build job **1031778020** (`运行 p0回归自动化 case`) consumes that variable.
  This declaration is from the read-only live pipeline configuration; the stage build
  detail itself exposes no get_bgwagent value and cannot prove a historical override.
- New-case configuration stage **8175789** / build stage **645275913** has
  `upstreamStageId=4875670`, so its shell consumes the same ordinary P0 stage's declaration.
  Its own static stage input is get_bgw_test_case. Both stages expose manualParams null /
  hasDynamicParams false; G8 params {} does not prove dynamic resolution.
- The release URL uses `/releases/2508.20.1.1/files`, not a numeric `/nodes/` identifier.
  Release `productNodeRevision` is **9cb2cd010e6a4a14974260f1a6571bc2**. Associated compile
  **108964467** / job **818597282** exposes node **85020052**; that association must not
  substitute the compile URL for the verified release product.

Before calling iPipe, persist a durable intent whose identity is `run_id + task_id + G8 action + input_hash`; after the remote action, persist a receipt containing the intent identity, rerun/manual-continuation identity, pipeline/build/stage/job IDs, exact revisions/profile/environment fingerprints, and result evidence. Completion requires a verified remote result for the same revisions/profile and a persisted `ipipe-evidence` update linked to both the intent and receipt. The rerun idempotency key is `ipipe.rerun:{run_id}:{stage_build_id}:{approval_id}`, so each G8 authorisation is one attempt and the same stage can be re-run again under a new G8; the earlier un-suffixed key stays valid only for the approval that created it. A new attempt requires every earlier attempt on that stage to have a receipt, otherwise `QUERY_REQUIRED` and `--reconcile-only` (GET only, located by the supplied approval) comes first. A rerun write that errors persists a redacted `ipipe.rerun.write_error` observation (HTTP status, error type, top-level field names, business code, short URL/token-free fragment). `--reconcile-only` yields `OK` (started, receipt), `RERUN_NOT_STARTED_UNPROVEN` (after a 300s grace the stage is unchanged and has no successor; `abandon_eligible`, rejection not proven, never retryable) or `QUERY_REQUIRED` (still unknown). Only the second makes `abandon-intent` appropriate, and a re-run then needs a different approved binding because the same binding replays the abandonment. Replay of a completed intent is idempotent; an unknown result, missing receipt, approval timeout/rejection, hash/profile/revision drift, unauthorized parameter, or unbounded retry is a hard stop and requires recovery or a new human decision.

### G9 and G10

G9 is mandatory before release and binds the release evidence, revisions, environment fingerprint, version, and remote release action.

G10 is a parent-owned post-run action, not a delivery phase. Inputs are an archived, redacted `RunSummary`, an immutable optimization proposal/candidate (root cause, exact control-plane target files, expected benefit, risk, rollback, and allowed verification commands), and the candidate hash. The parent publishes a Comate/Infoflow approval bound to that exact candidate hash, then persists both the proposal receipt and result receipt through KnowledgeSync and an iCafe comment. `APPLIED` completes only after control-plane validation and result archival; `ROLLED_BACK` completes only after byte/hash restoration and result archival; `ARCHIVE_PENDING` is a non-terminal pause that permits only idempotent KnowledgeSync retry; `RECOVERY_REQUIRED` stops all new application attempts until the recorded recovery action succeeds. Approval timeout/rejection, candidate/hash drift, validation failure without verified rollback, missing archive receipt, or any target outside approved tom-autodev control-plane roots is a stop. Forbidden targets include business repositories/worktrees, project profiles/registries, iPipe pipelines/templates/profiles, release rules, external skills, and secrets. A G10 proposal never changes a business repository, project profile, iPipe pipeline, or release rule.

## Diagnosis environment guard

The DIAGNOSE draft check and completion read `PhaseProtocol._authoritative_failure_class`
for the IPIPE evidence the DIAGNOSE entry points at. It returns a class only on strong
evidence: a proven dependency fetch failure of the diagnosed failed stage itself, read
only from `ipipe.dependency-failure:{run}:{build}:{stage_build_id}:{module}:{attempt}`
(`environment_evidence`; appended per attempt during G8 material preparation or
execution only; authoritative only when the latest attempt got 403/404 on every exact
product candidate and the stage's own declaration consumes the module; 5xx or missing
statuses are advisory; an `ipipe-rebuild` probe is never recorded),
or a runtime class in `repair_policy.RUNTIME_ENVIRONMENT_CLASSES` / `ENV_*` (structured
platform fields, control-plane answers). The runtime's keyword heuristic only yields the
advisory `ENVIRONMENT_SUSPECTED`; legacy stored `ENVIRONMENT_FAILURE` is advisory too.
With an authoritative class, routes in `repair_policy.ENVIRONMENT_FORBIDDEN_ROUTES`
(`REPAIR`, `SPEC`, `GRILL`) are refused with `ENVIRONMENT_REPAIR_FORBIDDEN`; the schema
applies the same routes to a diagnosis that reports an environment class itself.
