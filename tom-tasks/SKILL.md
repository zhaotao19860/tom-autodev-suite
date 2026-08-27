---
name: tom-tasks
description: Use when an approved Spec must be decomposed into an acyclic DAG of independently verifiable end-to-end capability slices across business repositories and an independent product test repository.
---

# Tom Tasks

## Overview

Turn behavior into a small frontier of end-to-end capability slices. A task is not a repository layer, module, test-only change, or final regression phase; it is one externally observable behavior that can obtain its own iPipe evidence.

## Task Contract

Each task must contain:

- `task_id`, one-sentence user/system behavior, and linked iCafe acceptance criteria/Spec behavior.
- Required business repositories and product test repository changes for that behavior.
- External test interface, test IDs/fixtures, expected evidence, and iPipe stages.
- Real blocking dependencies, risk/rollback boundary, and a completion predicate.
- A size that a fresh agent context can implement and Review without rediscovering architecture.

Keep business and test changes in one atomic Change Set. A task may touch multiple repositories when that is required for the same behavior.

Follow [`references/phase-contract.md`](references/phase-contract.md). Emit a schema-validated `task-dag` ArtifactEnvelope with predecessor hash, KU receipt, and iCafe comment receipt; the parent owns G3 approval.

## Decomposition Rules

1. Start from Spec behaviors and acceptance criteria, not repository names.
2. Group the smallest complete path from input through the existing external boundary to the product assertion.
3. Put compatibility or migration behavior in the same slice when it is required to keep the boundary safe.
4. Add a dependency only when the predecessor supplies a real interface, data contract, environment ability, or migration step.
5. For broad refactors use expand-contract: add compatible interface, migrate callers, then remove the old path in a later slice.
6. Do not create separate “implement x86bgw”, “implement bgwagent”, “write tests”, or “run regression” tasks for one behavior.

## Validation and Gate

Validate that the DAG is acyclic, every acceptance criterion is covered, every task has business/test/evidence fields, and every blocker is explained. Mark a task independently `PASS` or `FAIL` only through its planned iPipe stages. Ask for G3 approval of the complete DAG before `WorkspaceGate` or `tom-plan`.

Do not choose file-level implementation details, generate code, run project tests, or call iCode/iPipe in this phase.

**REQUIRED PARENT:** Return the DAG and coverage report to `tom-autodev`; preserve the Spec and input hashes.
