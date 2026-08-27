---
name: tom-implement
description: Use when an approved Task Plan must be executed to generate coordinated business-repository and independent product-test patches for one end-to-end task.
---

# Tom Implement

## Preconditions

Require G4 approval for the exact `Task Plan`, matching input hash, `WorkspaceGate` evidence, owned worktrees, repository baseline revisions, language/project rules, and traceability context. If any is absent or changed, return `PLAN_INCOMPLETE` or `INPUT_HASH_MISMATCH` and generate no code.

Follow [`references/phase-contract.md`](references/phase-contract.md). Emit a schema-validated `change-set` ArtifactEnvelope and return it to the parent for KU/iCafe persistence and G5 approval.

## Procedure

1. Read only the approved plan and its cited Spec, decisions, knowledge, impact, and test precedents.
2. Work in the task-owned business and product-test worktrees. Preserve all user changes and reference the recorded baseline.
3. Execute each plan checkbox exactly once. Generate the smallest business change and the corresponding independent product-test change for the same behavior and `change_set_id`.
4. Produce a complete diff, file list, traceability delta, test IDs, and input/content hashes.
5. Run `EvidenceGate` for plan coverage, repository ownership, change-set atomicity, and hash consistency.
6. Stop for G5 approval of the complete candidate diff before applying or submitting it.

## Hard Boundary

The Mac control plane may generate and inspect source, fixtures, and diff, but must not run project compilation, unit tests, regression, integration, Docker/NCS build scripts, or local test substitutes. Put all execution parameters in the Task Plan for iPipe. Do not call iCode or iPipe from this phase.

## Output

Return a candidate `Change Set` containing business/test patches, complete diff, changed symbols, test IDs, traceability, plan coverage, evidence references, and the exact approval input hash. Never silently expand scope, invent an interface, or repair a plan conflict; return `PLAN_INCOMPLETE` to `tom-plan` instead.

**REQUIRED PARENT:** Return the candidate to `tom-autodev` for EvidenceGate and G5 approval.
