---
name: tom-autodev
description: Use when a user wants to drive an iCafe requirement through Spec, end-to-end task implementation, source review, iCode, iPipe verification, repair, and release across a configured project and language. Also use when a failed or manual iPipe stage such as BGW P0新case回归 must be parameterized and rerun through G8.
---

# Tom Autodev

## Suite Maintenance Review

When asked to review, re-review or judge completion of **tom-autodev-suite itself**,
read [`../docs/WORKFLOW_EXIT_CRITERIA.md`](../docs/WORKFLOW_EXIT_CRITERIA.md) from the
real suite directory (resolve an installed skill symlink if needed). Use its baseline,
evidence and exit rules; do not start an iCafe run for a suite audit. This maintenance
mode does not replace the business workflow, approvals or phase contracts below.

## Overview

Use this skill as the control-plane entry for a run. Keep project/language knowledge in child skills, put side effects behind approvals, and accept only iPipe execution evidence.

This is a Comate-only entrypoint. There is no alternate host or entrypoint. The runtime has no standalone release component; release is an approved terminal controller action backed by remote evidence.

## Project Registration (Setup)

Register a new project or language once before this skill can process its requirements. Setup is a one-time, human-supervised operation that produces a validated profile; it never starts a run, generates code, submits iCode, or triggers iPipe. Stable per-project facts ship as a template at `tom-autodev/templates/<project>.template.yaml`; each requirement derives its own profile at `~/.tom-autodev/config/projects/<project>/<CARD>.yaml` (one file per card, so concurrent requirements never clash) via `scripts/profile_wizard.py`, which asks only the changed parts (project, KB parent, repos/branches, revisions, members) and derives the rest. `review_provider` is optional — review is produced by `tom-review`, not an external command. Only a `READY` profile allows `start`. See [`references/setup.md`](references/setup.md).

## Start and Continue

Use this as the ordinary command path; run commands from the suite root (or use its installed `cli.py` path):

| User intent | Action |
|---|---|
| Start a confirmed requirement | `python3 tom-autodev/scripts/cli.py process CARD PROJECT` |
| See progress | `status CARD_OR_RUN` |
| Inspect pinned inputs and current model job | `context CARD_OR_RUN` |
| Continue after approval or a pause | `continue CARD_OR_RUN` |
| Stop a run | `stop RUN_ID` |
| Explicitly restart a stopped requirement | `start --fresh CARD PROJECT` |

Confirm the project and iCafe card with the user before starting. If the card maps to multiple runs, show the candidates and ask which run to use; never pick one from recency or chat context. `process` reads the iCafe snapshot and starts/drives a new run only when no history exists; otherwise it continues the unique active run for the specified project. A terminal run returns `RUN_ALREADY_TERMINAL` without restarting; `PROJECT_MISMATCH` requires correcting the project. `start` + `drive` remains available for separate steps. A `PROJECT_NOT_READY` response means setup is required; see [`references/setup.md`](references/setup.md).

### Comate command aliases

When the user writes `/tom-autodev CARD ACTION`, interpret the natural-language action
according to [`references/chat-commands.md`](references/chat-commands.md):

- `开始` → `process CARD PROJECT`, which creates/continues and drives to the next
  ProducerJob, approval, remote wait, terminal state, or block. It is not the same as
  bare `start`, which only creates a run.
- `结束` → `stop CARD_OR_RUN`, preserving the audit trail.
- `状态` or `当成状态` → `status CARD_OR_RUN`.
- `继续` → `continue CARD_OR_RUN`, not read-only `resume`.
- `重新开始` → stop any selected active run, then `start --fresh CARD PROJECT` and
  `drive` the new run. Never delete the old event/approval/artifact records.

The project may be omitted only when it is read from a unique active run, a single
confirmed card profile, or an explicit current-turn confirmation. Never infer it from
the card prefix, checkout directory, or recency. Multiple candidates must be shown
before any action.

`process`, `drive` and `continue` let WorkerDriver own sequencing and stop at a ProducerJob, approval, remote wait, terminal state, or actionable block. When it returns a ProducerJob, load that job's pinned inputs, result schema, and applicable child skill, produce only its DraftContent, and submit it with `submit-draft CARD_OR_RUN JOB_ID DRAFT.json`. The CLI automatically requests the returned gate and exact `approval_input_hash`, returning `PARKED / APPROVAL_WAIT` after delivery; it never approves it. If delivery fails, use `continue` to retry with the saved candidate; do not change or regenerate it. Direct Python worker/bridge callers still handle `APPROVAL_REQUIRED` through the host approval adapter. After approval, call `continue` so the worker consumes the handoff and saved draft.

When `status` receives a card ID, it prefers the unique active run and falls back to the unique terminal run when no active run exists. If several candidates remain, show them and ask the user for an explicit run ID; never select a candidate silently. `drive`, `continue`, and `stop` resolve card IDs only to active runs.

`context CARD_OR_RUN` reads pinned action inputs, schema and the current ProducerJob (including saved draft state), without executing a phase, recovering SUBMIT or creating a job. Load referenced artifact content with `artifact show ARTIFACT_ID`; honor the job mode, including `{spec, dag}` for merged SPEC.

`resume RUN_ID` remains a read-only checkpoint and uncertain-intent inspection command. It does not drive a run. To restart a stopped requirement, first stop/verify the old run and use `start --fresh`; this creates a new auditable run without deleting the old events, approvals or artifacts. Do not use `next` or `complete-phase` to sequence normal work; advanced recovery and manual controls are in [`../docs/OPERATIONS.md`](../docs/OPERATIONS.md). Keep iCafe/project confirmation, human approvals, and final delivery confirmation with the user.

Do not infer projects from directories or bind iCafe cards without confirmation.

A card may start with no acceptance criteria. Accept it, let `tom-grill` agree the criteria with the requirement owner into `acceptance_delta`, and never rewrite the snapshot. Spec traceability must cover the union of snapshot `acceptance` and `acceptance_delta`; an empty union stops the run with `ACCEPTANCE_CRITERIA_MISSING`.

## Workflow

For downstream product inputs, decide each module's `changed` status from this run's
frozen Change Set / `pipeline_plan.product_inputs`; a clean worktree is not evidence
of no changes. Unchanged modules require a successful release job of the target
branch's **BranchPipeline**, with verifiable branch/source revision and an authenticated
download check. If unavailable, prepare only an adapter-proven **compile-only**
BranchPipeline action with release excluded, exact G8 binding, durable intent and
query-only recovery for unknown outcomes. Changed modules require this run's owned
**ChangePipeline** successful compile for the exact candidate revision and branch.
Never fall back to arbitrary historical compile, latest build, or a fixed URL.

Read stage, job/build declarations and upstream dependency inputs even when
`manualParams` is empty. A profile mapping is not a declaration. Override declared
task inputs only when the adapter proves exact scope/precedence without changing
the pipeline template. The current HTTP adapter cannot prove job override or
compile-only triggering and explicitly blocks with `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`
or `COMPILE_ONLY_TRIGGER_UNSUPPORTED`. Do not claim a real rerun was repaired.
See [`references/phase-protocol.md`](references/phase-protocol.md) for measured API limits.

Advance one frontier task at a time:

```text
Intake -> tom-grill -> tom-spec -> tom-tasks -> WorkspaceGate
       -> tom-plan -> tom-implement -> EvidenceGate -> tom-review
       -> iCode -> iPipe -> approved release
```

The `WorkerDriver` owns sequencing. After a gate settles, it runs every deterministic
step itself — `next` action computation, EvidenceGate/transition validation, and the
controller side effects (WORKSPACE bind, iCode submit, iPipe trigger/monitor, release
verify, KU/iCafe persistence, evidence ingestion) — and parks only on an **ApprovalJob**
(a human APPROVE bound to the exact `input_hash`) or a **ProducerJob** (model-authored
`DraftContent` for a skill phase: GRILL/SPEC/TASKS/PLAN/IMPLEMENT/REVIEW). A bounded
Agent turn fills a parked ProducerJob and submits it with `submit-draft`; the worker
builds the `ArtifactEnvelope`, runs EvidenceGate and `complete-phase`, and advances. The
Agent turn produces `DraftContent` only — it never runs a controller, `complete-phase`,
or a transition by hand, and never re-drives a step the worker already owns. REVIEW is
evidence-only and does not open a phase gate. Do not invent or require a host-side
LLM/Implement runner; the bounded Agent turn and its Read/apply_patch tools are the
producer backend (a seam designed to later swap to a headless-LLM backend without
changing the worker). For IMPLEMENT, the producer turn generates the real change-set and
commits owned worktrees with the repository identity and a Change-Id via
`submit_descriptor._commit_if_dirty` before submitting the draft; the worker takes G5,
`complete-phase` and the transition to REVIEW from there. Track progress by
`run_id/source_event_id/action_id/input_hash` and the generated artifact hash. If the
same action has not advanced, perform its missing work instead of repeating resume or
approval; reuse an existing pending/approved gate only for its exact approval input
hash. Re-read the saved candidate after approval; do not regenerate it or request G5
again. If blocked, report the actual missing input, permission, ownership, or evidence,
not a fictitious runner. Status must distinguish `待生成` from `已批准待提交`. G4/G5 的已批准待提交是 complete-phase；G7 的已批准待提交是 cli.py submit，不是 complete-phase。

Require approval for Spec/test interfaces/environment, Task DAG, each Task Plan, diff, every repair, iCode submission, iPipe rerun/manual continuation, and release. Bind approvals to input hash; Comate and Infoflow share one `approval_id`, and the first valid response wins. Every approval card must identify the affected repository/module, target branch, and pinned revision; a multi-repository change lists one row per repository and revision. G7 cards must also state whether the operation creates a new CR or appends an existing one.

All approval-facing text is Chinese by default: card titles, questions, material summaries,
button labels, reply examples, timeout notices, resend notices, acknowledgements and
error prompts. Display `同意 <approval_id>`, `需修改 <approval_id> <中文修改意见>` and
`停止 <approval_id>` for new approvals; the reject button reads `停止run`. Keep `APPROVE`,
`REVISE`, `REJECT` and the historical `驳回` label only as internal ledger values and
backward-compatible input aliases; never make new approvers translate an English command.
A reviewer may also give the feedback to Comate directly with
`/tom-autodev CARD 需修改 <意见>`, which settles a still-pending gate as a REVISE on the
comate channel and re-enters the phase in one step — no 如流 tap first.

When a run needs a human decision, use Comate's structured question interface first:
show each blocking choice as a题目 with background, explicit options, recommendation,
impact, evidence, and an `其他（请补充）` option when appropriate. Use multiple
questions in one round only when they are independent; ask dependent questions in
later rounds after the prior answer is known. Do not make the user extract the
question from a long paragraph. Map answers into the child phase DraftContent and
keep unanswered items in `choice_questions` / `unresolved_frontier`; never infer a
default answer. This interaction is still separate from approval: the worker creates
G1 only after the final answered `decision-log` is submitted, and the user must
approve that exact hash-bound result. Mirror the question and the final result in
如流 when needed (`scripts/ask_infoflow.py`, see `references/approval-policy.md`);
the weaker non-gate helper must never replace the approval ledger.

Collaboration group creation is a durable external write. Treat a missing local receipt as an
unknown outcome: the gateway must persist a redacted receipt keyed by the exact run intent, and
`continue` must query that receipt before phase recovery blocks the worker. A successful create
needs a valid `group_id`; `bot_id` may be absent when the platform does not echo the robot identity.
Never search by group name heuristically or issue a second create. A legacy run without a gateway
receipt remains `QUERY_REQUIRED` until an operator supplies an exact group ID for a controlled,
audited adoption.

All KU reads and writes go through the installed `/ku-doc-manage` skill entry point. The KU
adapter discovers it from `TOM_AUTODEV_KU_BINARY`, then `TOM_AUTODEV_KU_SKILL_DIR` or
`COMATE_SKILL_DIR`, and finally the standard personal skill location. It passes the resolved
operator as `BAIDU_CC_USERNAME`. The adapter accepts the skill's bounded debug preamble before
the JSON response, while still rejecting non-JSON output from other CLI adapters. If the KU
entry point or identity is unavailable, report that concrete prerequisite instead of collapsing
it into `KU_QUERY_UNKNOWN`.

Approval settlement creates one durable, hash-bound resume handoff containing `approval_id`, `run_id`, `input_hash`, the source event, and the next action. In a live Comate turn, register `scripts/ide_turn_hook.py` as the `Stop` hook in `~/.comate/hooks.json` or `~/.comate/hooks.local.json`: after a valid APPROVE it returns a blocking continuation with `additionalContext`, so Comate resumes without a manual `继续`. The hook only hands control back to Comate; it never executes a phase or external write. Replays are idempotent and stale, rejected, expired, mismatched, or completed handoffs are ignored. `SessionEnd` is fire-and-forget and cannot create a new Agent turn, so an ended session receives the existing 如流 fallback notice; `orchestrator.py notify-ide-turn` remains the manual notification path.

Call `EvidenceGate` before every checkpoint or external side effect. Block on a changed input hash, missing artifact, unresolved finding, revision mismatch, stale environment fingerprint, or stale verification evidence.

`tom-review` produces evidence only and never grants approval. A dual-axis PASS is a prerequisite for the parent-owned G7 iCode approval, bound to the exact reviewed Change Set hash; `INCOMPLETE`, `NEEDS_CLARIFICATION`, stale/hash-mismatched baselines, and blocking findings stop submission or route to diagnosis.

For a failed or manual iPipe stage, load the project skill's runtime/parameter map first, then use `orchestrator.py ipipe-rerun RUN STAGE_BUILD_ID APPROVAL_ID INPUT_HASH --parameter NAME ...`; it is the explicit G8/manual-continuation path and reuses the durable stage ownership check. The project skill names the topology, logs, version/counter checks, and parameter-to-product mapping; this controller resolves product URLs, redacts tokens, requests G8, reruns, and classifies the new evidence. Child project skills never call iPipe.

If the failed build's compile product can no longer be downloaded (the platform answers 403/404), stage re-runs cannot succeed; use `orchestrator.py ipipe-rebuild RUN --reason ...` (read-only check, then `--request-g8`, then `--approval-id A --input-hash H`). It builds the same CR revisions once more under G8, rebinds the run to the new build and supersedes the pending DIAGNOSE job; it needs the `DIAGNOSE -> IPIPE` edge, so an older pinned run is migrated first with `migrate-workflow-spec`. A superseded build (and a build rejected after `TRIGGER_MODE_MISMATCH`, marked `ipipe.build-rejected`) is never module, release or G9 evidence, and its stages cannot be re-run (`STAGE_BUILD_SUPERSEDED`). From `IPIPE` the replaced build must have failed or have its product proven gone.

A stage re-run counts as started only when the stage carries a start time later than the intent's creation (30 s skew) and is not a successor another re-run already receipted; otherwise the outcome stays unknown. `abandon-intent --local-error-proven` needs an observation recorded with `before_dispatch: true`.

To continue an approved CODE_ONLY repair of a `STOPPED` run in a fresh run, use `orchestrator.py adopt-code-only-repair SOURCE TARGET TASK DIAGNOSIS_ARTIFACT PLAN_ARTIFACT`. It accepts only the task's latest diagnosis and its accepted Plan of the current DAG, refuses a source whose repair budget is spent (`ADOPTION_BUDGET_EXHAUSTED`), requires a task-bound submission receipt, replays idempotently and refuses a different pair (`ADOPTION_CONFLICT`). The target inherits the source's spent rounds and enters IMPLEMENT through a recorded controlled decision (no new workflow edge).

The platform's own review (小码哥) is a second opinion taken after SUBMIT, while the CR exists and nothing is merged: `orchestrator.py ai-review start RUN --change-number N --revision R`, then `ai-review poll RUN --conversation-id C`. The conversation id comes back once and `get_ai_review` accepts nothing else, so the trigger claims an intent and writes the id into a receipt; a trigger left without one reports `AI_REVIEW_CONVERSATION_LOST` instead of spending a second review to replace a lost one. Verify each finding against the code before changing anything: a report that cannot see the sibling repositories will call a cross-repository contract broken when the other side already enforces it. A finding you do confirm routes `SUBMIT -> DIAGNOSE` and repairs through the ordinary path, landing as a new revision on the same CR; the repaired task owes iCode that new change set before IPIPE will start.

## Hard Boundaries

- On Mac, only read code/knowledge, generate artifacts, inspect impact, perform source Review, manage worktrees, and parse iPipe evidence.
- Return `LOCAL_EXECUTION_FORBIDDEN` for every local project build or test request.
- Never run project compilation, unit tests, regression, integration, Docker/NCS build scripts, or a local substitute. Only the configured iPipe runner may produce project execution evidence.
- Never overwrite user changes, alter pipeline templates at runtime, persist secrets, merge CRs automatically, or use raw Git push as an iCode fallback.
- Require `WorkspaceGate`; missing matching baseline evidence returns `BASELINE_UNVERIFIED`.
- Classify Review findings as `CONFIRMED`, `REJECTED_WITH_REASON`, or `NEEDS_CLARIFICATION`. Do not implement an unverified suggestion.
- Route `CODE_FAILURE`, `TEST_FAILURE`, and confirmed blocking Review findings to `tom-diagnose` before repair. Environment failures never create business patches.
- Return `DIAGNOSIS_INCOMPLETE` when root cause or verification is missing. Stop after two no-progress rounds per signature, force architecture Review after three failed fixes, and cap repair at five rounds. An approved architecture Review resets only the first two counters (`NO_PROGRESS`, `THREE_FAILED_FIXES`); the five-round `REPAIR_LIMIT` counts every confirmed round of the run, across reviews and including rounds inherited through `adopt-code-only-repair`. When the failure has an authoritative environment classification -- strong evidence only: structured platform fields (a candidate mapping not yet verified against real iPipe responses; no observed response carries them, so it does not take effect today), a control-plane-proven dependency fetch failure of the diagnosed failed stage itself (latest attempt 403/404 on every exact candidate of a product that stage's declaration consumes; 5xx, missing statuses and `ipipe-rebuild` probes are never authoritative), or `ENV_*`/`PIPELINE_TRANSIENT`/`JOB_PARAMETER_OVERRIDE_UNSUPPORTED`/`COMPILE_ONLY_TRIGGER_UNSUPPORTED` -- a diagnosis may not route `REPAIR`, `SPEC` or `GRILL` (`ENVIRONMENT_REPAIR_FORBIDDEN`). Keyword hints (`ENVIRONMENT_SUSPECTED`) are advisory.

## Child Skills

| Phase | Skill |
|---|---|
| Clarify / Spec / DAG | `tom-grill`, `tom-spec`, `tom-tasks` |
| Plan / implement / Review | `tom-plan`, `tom-implement`, `tom-review` |
| Diagnose | `tom-diagnose` |
| Language | `tom-lang-c-cpp` or `tom-lang-npl` |
| Project | `tom-project-bgw` or `tom-project-xflow` |

Project skills supply repository topology, environment, iPipe parameter names, log locations, and version/counter inspection. This controller sequences `next` / G8 / `ipipe-rerun` / evidence ingest. For BGW, read `tom-project-bgw/references/runtime-topology.md` before filling or rerunning `P0新case回归`.

Every child phase follows [`references/phase-protocol.md`](references/phase-protocol.md): consume and emit a schema-validated, content-hashed `ArtifactEnvelope`, persist the human-readable phase document to KU, and link only the required milestone/evidence to iCafe. Child skills return artifacts and evidence to this controller; only this controller owns iCafe/KU/iCode/iPipe adapters and G0-G10 gates.

### Knowledge-base collaboration contract

KU is the human-facing collaboration record for a run. It must let a requirement owner,
developer, tester, reviewer, or a new owner understand and take over the work without
opening the control-plane database or reading raw JSON.

For every phase that produces a requirement, decision, specification, task, workspace,
plan, change, review, diagnosis, iPipe, or release artifact:

- Publish a readable Markdown child document under the run root. Do not publish only a phase
  title, a field summary, or a JSON dump.
- The document must explain the one-line conclusion, purpose, upstream inputs, actual
  analysis/design/change/verification content, confirmed items, outputs, upstream/downstream
  relationship, risks/open items, and a hand-off checklist.
- Put canonical JSON, content hash, input hash, approval binding, artifact id, revision,
  iCafe/iPipe links, and external receipts in the audit section and canonical appendix.
- Keep the body useful for comments and review. Large diffs, raw HTML, and unbounded logs
  remain in ArtifactStore; the KU page shows file-level summaries, counts, reasons, tests,
  and controlled references to the complete artifact.
- If an artifact or evidence does not exist, state that it is not yet produced or is
  unknown. Never fill a historical document with guessed design, code, test, delivery, or
  release facts.

The run root is a reading map, not just an index. It must describe the run purpose, card,
current status/blocked state when known, reading order, the 00–11 document set, each phase's
question/purpose/status/handoff, and current collaboration items. See
[`references/phase-artifacts.md`](references/phase-artifacts.md) for the document map and
artifact contract.

When a reviewer selects “需修改” on G1–G6, do not tell them to restart and do not
reissue the same gate manually. The next `/tom-autodev CARD 继续` consumes the
hash-bound feedback, re-enters the producer phase, and delivers a new Chinese approval card
for the new content hash. G4 is the PLAN producer gate and is revisable for the same reason
as the earlier producer gates. Historical English `REJECT` replies remain compatible.
For an explicit CLI recovery, run `scripts/cli.py revise RUN_ID --approval-id <rejected>
--feedback <text> --actor <who>`; the producer is re-entered with
`revision_context` (instruction plus the previous draft) from `context`, must address every
point of the instruction, keep what was not objected to, and the new gate shows a diff. When
an earlier phase or a repository fact must change, open `scripts/cli.py rewind RUN_ID --to
<GRILL|SPEC|TASKS> --reason ... --actor ... [--previous-profile COPY] --request`, wait for the
human `CHANGE_REQUEST` approval, then apply it with `--approval-id`. Never move a run by hand.

A requirement the user changes after G1 is never absorbed into a later draft. From PLAN or
IMPLEMENT, report it as `USER_REQUIREMENT_CHANGED` (G5 “需修改” first re-enters IMPLEMENT so
a pending job exists); the diagnosis routes `GRILL` and G6 re-opens the decision log. Tell
the user what a draft contains only from the saved draft, never from the intended version.

If a paused run returns `WORKFLOW_SPEC_DRIFT`, do not edit its event or hash in place and do
not use ordinary `continue` to bypass the guard. Use the dedicated
`scripts/cli.py migrate-workflow-spec RUN_ID --request` gate, then apply the exact approved
`approval_id` with `--approval-id`, or call `continue` to consume an existing APPROVE whose
run, action and input_hash exactly match the current migration plan. `continue` never
approves or reopens the gate; stale, rejected or cross-run approvals still cannot pass
drift. `status` remains read-only and shows “迁移已批准待应用”, with `continue` as the
next step. This appends an auditable workflow-pin migration receipt
and preserves the run's phase artifacts, existing approval bindings, and external intent
records. Only after that migration is approved may an unknown approval delivery use
`confirm-approval-redelivery`; that command still reuses the original approval_id/input_hash
and never creates a new G1.

For a legacy run, use `scripts/backfill_knowledge.py RUN_ID CARD_ID --project PROJECT
--username USERNAME`. It reads only integrity-checked phase artifacts, publishes KU-only
`-human-readable-r2` documents under a separate run root, and reports missing phases.
It must not alter run state, repin workflow policy, create approvals, or emit duplicate
iCafe comments. Use `--dry-run` before any external write.

Persist events, artifacts, approvals, locks, heartbeats, external results, and handoffs. Resume only from a confirmed checkpoint; never submit, notify, rerun, or release twice.

## Post-Run Optimization (G10)

After a terminal run, build `RunSummary` from durable events, approvals, phase artifacts, collaboration receipts, and iPipe evidence. It must redact secrets and nonessential personal data. A candidate optimization must identify root cause, exact target files, expected benefit, risk, rollback, and control-plane-only verification commands.

Only a proposal created from archived run evidence may be applied. Bind a `G10` approval to the exact candidate hash, require an approved Comate/Infoflow ledger record, constrain every target to approved tom-autodev roots, and reject business-repository, project-profile, iPipe, or pipeline changes. Archive proposal and result through `KnowledgeSync`; rollback every changed file if validation fails. Never use G10 to modify a production repository, profile, or pipeline template.

## References

- Read `references/state-machine.md` for transitions and recovery.
- Read `references/phase-artifacts.md` for phase contracts.
- Read `references/approval-policy.md` before requesting approval.
- Read `references/failure-taxonomy.md` before handling failure.
- Read `references/setup.md` to register a project before its first run.
- Read `references/project-registry.md` during setup or project selection.
