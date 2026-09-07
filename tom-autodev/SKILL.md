---
name: tom-autodev
description: Use when a user wants to drive an iCafe requirement through Spec, end-to-end task implementation, source review, iCode, iPipe verification, repair, and release across a configured project and language.
---

# Tom Autodev

## Overview

Use this skill as the control-plane entry for a run. Keep project/language knowledge in child skills, put side effects behind approvals, and accept only iPipe execution evidence.

This is a Comate-only entrypoint. There is no alternate host or entrypoint. The runtime has no standalone release component; release is an approved terminal controller action backed by remote evidence.

## Start and Resume

1. Require an explicit project and human-confirmed iCafe card.
2. Load project/language skills and profile.
3. Read the iCafe snapshot through the iCafe boundary, then call `start(requirement_id, project, requirement_snapshot=...)`; use `status`, `approve`, `resume`, or `stop` for an existing run.
4. Return `PROJECT_NOT_READY` when the profile, independent test repo, Review provider, stable iPipe profile, environment profile, or approval channel is missing.

Do not infer projects from directories or bind iCafe cards without confirmation.

A card may start with no acceptance criteria. Accept it, let `tom-grill` agree the criteria with the requirement owner into `acceptance_delta`, and never rewrite the snapshot. Spec traceability must cover the union of snapshot `acceptance` and `acceptance_delta`; an empty union stops the run with `ACCEPTANCE_CRITERIA_MISSING`.

## Workflow

Advance one frontier task at a time:

```text
Intake -> tom-grill -> tom-spec -> tom-tasks -> WorkspaceGate
       -> tom-plan -> tom-implement -> EvidenceGate -> tom-review
       -> iCode -> iPipe -> approved release
```

Require approval for Spec/test interfaces/environment, Task DAG, each Task Plan, diff, every repair, iCode submission, iPipe rerun/manual continuation, and release. Bind approvals to input hash; Comate and Infoflow share one `approval_id`, and the first valid response wins.

When a run needs a decision that is not a gate, ask it in 如流 as well as in the IDE (`scripts/ask_infoflow.py`, see `references/approval-policy.md`); a question that only exists in the CLI stays invisible until someone comes back to look.

Telling the operator that a run is parked is a session-stop concern, not a phase concern: register `scripts/ide_turn_hook.py` as the harness `Stop` hook (`~/.claude/settings.json`) so the notice follows the stop rather than a `complete-phase` call, which is how gated stops, questions, errors, and API-driven runs used to go unannounced. `orchestrator.py notify-ide-turn` is the same path by hand; one notice per ledger event, suppressed while an approval card is out.

Call `EvidenceGate` before every checkpoint or external side effect. Block on a changed input hash, missing artifact, unresolved finding, revision mismatch, stale environment fingerprint, or stale verification evidence.

`tom-review` produces evidence only and never grants approval. A dual-axis PASS is a prerequisite for the parent-owned G7 iCode approval, bound to the exact reviewed Change Set hash; `INCOMPLETE`, `NEEDS_CLARIFICATION`, stale/hash-mismatched baselines, and blocking findings stop submission or route to diagnosis.

The platform's own review (小码哥) is a second opinion taken after SUBMIT, while the CR exists and nothing is merged: `orchestrator.py ai-review start RUN --change-number N --revision R`, then `ai-review poll RUN --conversation-id C`. The conversation id comes back once and `get_ai_review` accepts nothing else, so the trigger claims an intent and writes the id into a receipt; a trigger left without one reports `AI_REVIEW_CONVERSATION_LOST` instead of spending a second review to replace a lost one. Verify each finding against the code before changing anything: a report that cannot see the sibling repositories will call a cross-repository contract broken when the other side already enforces it.

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
- Read `references/project-registry.md` during setup or project selection.
