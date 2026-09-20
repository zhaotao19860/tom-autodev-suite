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

| Phase | Required inputs | Output and receipts | Completion predicate | Approval ownership/binding | Stop conditions |
|---|---|---|---|---|---|
| `WORKSPACE` | G3-approved `task-dag`, selected frontier task, project profile, repository ownership and baseline evidence | WorkspaceGate receipt with owned worktree IDs, exact business/test baseline revisions, lock/cleanup evidence, and content hash | Both worktrees are owned, clean against the recorded baseline, and all required repositories/test profile are verified; the business worktree is the repo the task's `business_module` names (a multi-repo run selects by module, not `business_repos[0]`); receipt is a prerequisite for Plan | No new approval: G3 remains approval of the complete Task DAG/frontier; G4 is exclusively parent approval of the completed exact Task Plan hash | `BASELINE_UNVERIFIED`, ownership/lock conflict, missing test repository, `WORKSPACE_TASK_REPO_UNRESOLVED`/`WORKSPACE_TASK_REPO_MISMATCH`, stale graph/profile, or hash drift |
| `SUBMIT` | G7-approved dual-axis Review PASS, fixed Change Set hash, repository revisions, and submission policy | iCode submission receipt containing CR IDs, revision set ID, target branches, business/test revisions, and submission hash | iCode confirms the exact approved revision set once and emits a durable receipt | Parent owns G7 through Comate/Infoflow; approval input hash equals the reviewed Change Set/submission hash | Review non-PASS, stale/mismatched baseline or hash, missing receipt, iCode rejection/unknown result, or duplicate intent |
| `IPIPE` | iCode submission receipt, pinned pipeline/module/release profile, business/test revisions, environment fingerprint, and G7 binding | `ipipe-evidence` artifact with pipeline/build/stage/job IDs, revisions, profile/environment fingerprints, test results, logs/evidence refs, and terminal stage result; every module/build/stage mapping is represented in the remote refs | Every required remote build/unit/regression/integration stage returns a verified result for the exact revisions and profile; success advances to RELEASE | Parent owns G7 for the initial trigger and G8 for failed-stage rerun or manual continuation; each rerun approval binds the failed-stage evidence/input hash | Revision/profile/environment mismatch, missing or unverifiable evidence, `CODE_FAILURE`/`TEST_FAILURE` to DIAGNOSE, environment failure to ENVIRONMENT_BLOCKED, or pipeline transient without bounded retry |
| `RELEASE` | Verified successful iPipe evidence, release rule/version mapping, exact revisions, environment fingerprint, and G9 context | `release-evidence` receipt with release version, package/deployment identity, provenance, approver, and remote release result | Release action is confirmed once and the version/provenance matches iPipe evidence, then state becomes `RELEASE_SUCCESS` | Parent owns G9; approval binds exact release-evidence/input hash and revision/environment fingerprints | Missing/failed release evidence, version/provenance mismatch, environment failure, duplicate/unknown release result, or approval timeout/rejection |
| `RELEASE_SUCCESS` / `STOPPED` | Terminal result event and all preceding durable evidence (or explicit stop reason) | No next phase; final state receipt and immutable run summary | `RELEASE_SUCCESS` requires verified release evidence; `STOPPED` requires an explicit reason and persisted handoff | No new gate; G10 is available only after a terminal summary and is separately parent-owned | Any `next`, submit, rerun, release, or optimization attempt lacking the terminal predicate is rejected as `TERMINAL_STATE` |

### G8 Controller Action

G8 is a parent-owned controller action, not a child phase. Its inputs are the frozen failed-stage `ipipe-evidence`, the exact iCode submission/revisions, pinned pipeline/module/profile/environment identity, the requested failed stage, and an allowlisted rerun or manual-continuation parameter set. Before filling parameters, the parent loads the project skill's runtime map so each named input resolves to a compiled product of this run (for BGW, `get_bgw_test_case` → `baidu/nsiqa/x86bgw`, `get_bgwagent` → `baidu/sysip/bgwagent`). The parent publishes the same approval request to Comate and Infoflow; the approval `input_hash` is the canonical hash of the failed-stage evidence plus action and parameters, and cannot authorize a revision, profile, or unplanned stage change. After rerun, classify from the project skill's log order before routing to DIAGNOSE: job log, client `out`/`res.csv`, then server `/var/log/messages`.

Before calling iPipe, persist a durable intent whose identity is `run_id + task_id + G8 action + input_hash`; after the remote action, persist a receipt containing the intent identity, rerun/manual-continuation identity, pipeline/build/stage/job IDs, exact revisions/profile/environment fingerprints, and result evidence. Completion requires a verified remote result for the same revisions/profile and a persisted `ipipe-evidence` update linked to both the intent and receipt. Replay of a completed intent is idempotent; an unknown result, missing receipt, approval timeout/rejection, hash/profile/revision drift, unauthorized parameter, or unbounded retry is a hard stop and requires recovery or a new human decision.

### G9 and G10

G9 is mandatory before release and binds the release evidence, revisions, environment fingerprint, version, and remote release action.

G10 is a parent-owned post-run action, not a delivery phase. Inputs are an archived, redacted `RunSummary`, an immutable optimization proposal/candidate (root cause, exact control-plane target files, expected benefit, risk, rollback, and allowed verification commands), and the candidate hash. The parent publishes a Comate/Infoflow approval bound to that exact candidate hash, then persists both the proposal receipt and result receipt through KnowledgeSync and an iCafe comment. `APPLIED` completes only after control-plane validation and result archival; `ROLLED_BACK` completes only after byte/hash restoration and result archival; `ARCHIVE_PENDING` is a non-terminal pause that permits only idempotent KnowledgeSync retry; `RECOVERY_REQUIRED` stops all new application attempts until the recorded recovery action succeeds. Approval timeout/rejection, candidate/hash drift, validation failure without verified rollback, missing archive receipt, or any target outside approved tom-autodev control-plane roots is a stop. Forbidden targets include business repositories/worktrees, project profiles/registries, iPipe pipelines/templates/profiles, release rules, external skills, and secrets. A G10 proposal never changes a business repository, project profile, iPipe pipeline, or release rule.
