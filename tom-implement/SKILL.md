---
name: tom-implement
description: Use when an approved Task Plan must be executed to generate coordinated business-repository and independent product-test patches for one end-to-end task.
---

# Tom Implement

## Preconditions

Require the exact pinned Task Plan, parent-validated approval/mode, matching input hash, WorkspaceGate evidence, owned worktrees, baseline revisions and language/project rules. If required authorization, evidence or identity is absent or changed, return `PLAN_INCOMPLETE` or `INPUT_HASH_MISMATCH` and generate no code. Do not add a gate when the controller has resolved a different authorized mode.

Follow the [producer contract](../tom-autodev/references/producer-contract.md), [change-set schema](../tom-autodev/schemas/change-set.schema.json) and [phase-specific rules](references/phase-contract.md). Return `change-set` DraftContent; the parent packages receipts, takes G5 and commits the phase.

## Procedure

1. Read only the approved plan and its cited Spec, decisions, knowledge, impact, and test precedents.
2. Work in the task-owned business and product-test worktrees. Preserve all user changes and reference the recorded baseline.
3. Compare each planned step with the actual owned-worktree diff and existing candidate before editing. On resume, keep completed edits, finish only missing work and stop on unexplained user/conflicting changes. Do not replay a patch or commit just because a checklist lacks a completion mark. Generate the smallest business and product-test changes for the same behavior and `change_set_id`.
4. Commit dirty owned worktrees only through `submit_descriptor._commit_if_dirty(worktree, message)`. That helper uses the repository `user.name` / `user.email` and writes a `Change-Id` trailer. Do not invent a bot identity such as `tom-autodev@local`, and do not leave a dirty or unpushable HEAD for Review. Pin `revisions` to the resulting commits.
5. Produce a complete diff, file list, traceability delta, test IDs, and input/content hashes.
6. Record `deviations`: every place the diff departs from the approved plan, each with `from_plan`, `as_implemented` and `reason`. The field is required and an empty list is the assertion that the plan was followed exactly, so it is never omitted. `candidate_hash` covers it, which means a deviation cannot be added after G5 or a Review bound that hash — if one surfaces later, the change set is a new one.
7. Check source/diff coverage and report scope, ownership or evidence gaps; the worker runs EvidenceGate and semantic validation before completion.
8. Save and submit the candidate draft, then return control for G5. Source edits/local commits in owned worktrees are the review candidate, not an iCode submission or deployed change. On approval, reuse this candidate rather than re-generating it.

## Hard Boundary

The Mac control plane may generate and inspect source, fixtures, and diff, but must not run project compilation, unit tests, regression, integration, Docker/NCS build scripts, or local test substitutes. Put all execution parameters in the Task Plan for iPipe. Do not call iCode or iPipe from this phase.

## Output

Return a candidate `Change Set` containing business/test patches, complete diff, changed symbols, test IDs, traceability, plan coverage, deviations from the plan, evidence references, and the exact approval input hash. Never silently expand scope, invent an interface, or repair a plan conflict; return `PLAN_INCOMPLETE` to `tom-plan` instead.

**REQUIRED PARENT:** Return the candidate to `tom-autodev` for EvidenceGate and G5 approval.
