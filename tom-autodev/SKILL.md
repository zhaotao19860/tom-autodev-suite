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

Register a new project or language once before this skill can process its requirements. Setup is a one-time, human-supervised operation that produces a validated profile at `~/.tom-autodev/config/projects/<project>.yaml`; it never starts a run, generates code, submits iCode, or triggers iPipe. Only a `READY` profile allows `start`. See [`references/setup.md`](references/setup.md).

## Start and Continue

Use this as the ordinary command path; run commands from the suite root (or use its installed `cli.py` path):

| User intent | Action |
|---|---|
| Start a confirmed requirement | `python3 tom-autodev/scripts/cli.py start CARD PROJECT`, then `drive CARD` |
| See progress | `status CARD_OR_RUN` |
| Continue after approval or a pause | `continue CARD_OR_RUN` |
| Stop a run | `stop RUN_ID` |

Confirm the project and iCafe card with the user before starting. If the card maps to multiple runs, show the candidates and ask which run to use; never pick one from recency or chat context. `start` reads the iCafe snapshot and reuses the registered project profile. A `PROJECT_NOT_READY` response means setup is required; see [`references/setup.md`](references/setup.md).

`drive` and `continue` let WorkerDriver own sequencing and stop at a ProducerJob, approval, remote wait, terminal state, or actionable block. When it returns a ProducerJob, load that job's pinned inputs, result schema, and applicable child skill, produce only its DraftContent, and submit it with `submit-draft CARD_OR_RUN JOB_ID DRAFT.json`. For `APPROVAL_REQUIRED`, request the returned gate and exact `approval_input_hash` through `request-approval`; do not change or regenerate the saved candidate. After approval, call `continue` so the worker consumes the handoff and saved draft.

When `status` receives a card ID, it prefers the unique active run and falls back to the unique terminal run when no active run exists. If several candidates remain, choose by run ID. `drive`, `continue`, and `stop` resolve card IDs only to active runs.

`resume RUN_ID` remains a read-only checkpoint and uncertain-intent inspection command. It does not drive a run. Do not use `next` or `complete-phase` to sequence normal work; advanced recovery and manual controls are in [`../docs/OPERATIONS.md`](../docs/OPERATIONS.md). Keep iCafe/project confirmation, human approvals, and final delivery confirmation with the user.

Do not infer projects from directories or bind iCafe cards without confirmation.

A card may start with no acceptance criteria. Accept it, let `tom-grill` agree the criteria with the requirement owner into `acceptance_delta`, and never rewrite the snapshot. Spec traceability must cover the union of snapshot `acceptance` and `acceptance_delta`; an empty union stops the run with `ACCEPTANCE_CRITERIA_MISSING`.

## Workflow

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

When a run needs a decision that is not a gate, ask it in 如流 as well as in the IDE (`scripts/ask_infoflow.py`, see `references/approval-policy.md`); a question that only exists in the CLI stays invisible until someone comes back to look.

Approval settlement creates one durable, hash-bound resume handoff containing `approval_id`, `run_id`, `input_hash`, the source event, and the next action. In a live Comate turn, register `scripts/ide_turn_hook.py` as the `Stop` hook in `~/.comate/hooks.json` or `~/.comate/hooks.local.json`: after a valid APPROVE it returns a blocking continuation with `additionalContext`, so Comate resumes without a manual `继续`. The hook only hands control back to Comate; it never executes a phase or external write. Replays are idempotent and stale, rejected, expired, mismatched, or completed handoffs are ignored. `SessionEnd` is fire-and-forget and cannot create a new Agent turn, so an ended session receives the existing 如流 fallback notice; `orchestrator.py notify-ide-turn` remains the manual notification path.

Call `EvidenceGate` before every checkpoint or external side effect. Block on a changed input hash, missing artifact, unresolved finding, revision mismatch, stale environment fingerprint, or stale verification evidence.

`tom-review` produces evidence only and never grants approval. A dual-axis PASS is a prerequisite for the parent-owned G7 iCode approval, bound to the exact reviewed Change Set hash; `INCOMPLETE`, `NEEDS_CLARIFICATION`, stale/hash-mismatched baselines, and blocking findings stop submission or route to diagnosis.

For a failed or manual iPipe stage, load the project skill's runtime/parameter map first, then use `orchestrator.py ipipe-rerun RUN STAGE_BUILD_ID APPROVAL_ID INPUT_HASH --parameter NAME ...`; it is the explicit G8/manual-continuation path and reuses the durable stage ownership check. The project skill names the topology, logs, version/counter checks, and parameter-to-product mapping; this controller resolves product URLs, redacts tokens, requests G8, reruns, and classifies the new evidence. Child project skills never call iPipe.

The platform's own review (小码哥) is a second opinion taken after SUBMIT, while the CR exists and nothing is merged: `orchestrator.py ai-review start RUN --change-number N --revision R`, then `ai-review poll RUN --conversation-id C`. The conversation id comes back once and `get_ai_review` accepts nothing else, so the trigger claims an intent and writes the id into a receipt; a trigger left without one reports `AI_REVIEW_CONVERSATION_LOST` instead of spending a second review to replace a lost one. Verify each finding against the code before changing anything: a report that cannot see the sibling repositories will call a cross-repository contract broken when the other side already enforces it. A finding you do confirm routes `SUBMIT -> DIAGNOSE` and repairs through the ordinary path, landing as a new revision on the same CR; the repaired task owes iCode that new change set before IPIPE will start.

## Hard Boundaries

- On Mac, only read code/knowledge, generate artifacts, inspect impact, perform source Review, manage worktrees, and parse iPipe evidence.
- Return `LOCAL_EXECUTION_FORBIDDEN` for every local project build or test request.
- Never run project compilation, unit tests, regression, integration, Docker/NCS build scripts, or a local substitute. Only the configured iPipe runner may produce project execution evidence.
- Never overwrite user changes, alter pipeline templates at runtime, persist secrets, merge CRs automatically, or use raw Git push as an iCode fallback.
- Require `WorkspaceGate`; missing matching baseline evidence returns `BASELINE_UNVERIFIED`.
- Classify Review findings as `CONFIRMED`, `REJECTED_WITH_REASON`, or `NEEDS_CLARIFICATION`. Do not implement an unverified suggestion.
- Route `CODE_FAILURE`, `TEST_FAILURE`, and confirmed blocking Review findings to `tom-diagnose` before repair. Environment failures never create business patches.
- Return `DIAGNOSIS_INCOMPLETE` when root cause or verification is missing. Stop after two no-progress rounds per signature, force architecture Review after three failed fixes, and cap repair at five rounds.

## Child Skills

| Phase | Skill |
|---|---|
| Clarify / Spec / DAG | `tom-grill`, `tom-spec`, `tom-tasks` |
| Plan / implement / Review | `tom-plan`, `tom-implement`, `tom-review` |
| Diagnose | `tom-diagnose` |
| Language | `tom-lang-c-cpp` or `tom-lang-npl` |
| Project | `tom-project-bgw` or `tom-project-xflow` |

Project skills supply repository topology, environment, iPipe parameter names, log locations, and version/counter inspection. This controller sequences `next` / G8 / `ipipe-rerun` / evidence ingest. For BGW, read `tom-project-bgw/references/runtime-topology.md` before filling or rerunning `P0新case回归`.

Every child phase follows [`references/phase-protocol.md`](references/phase-protocol.md): consume and emit a schema-validated, content-hashed `ArtifactEnvelope`, persist changed requirement/design/implementation/review/diagnosis documents to KU, and link the artifact in an iCafe comment. Child skills return artifacts and evidence to this controller; only this controller owns iCafe/KU/iCode/iPipe adapters and G0-G10 gates.

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
