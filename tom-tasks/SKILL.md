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
- `business_module` naming exactly one registered business repository, plus changes in its independent product-test repository.
- External test interface, test IDs/fixtures, expected evidence, and iPipe stages.
- Real blocking dependencies, risk/rollback boundary, and a completion predicate.
- A size that a fresh agent context can implement and Review without rediscovering architecture.

Keep the selected business repository and independent test repository in one Change Set. The current runtime binds one business module per task. A requirement may span modules through independently verifiable compatible slices and explicit dependencies; if it requires an indivisible multi-business-repository task, report that runtime capability gap. Do not hide multiple business modules in a singular field or pretend a non-verifiable layer split is an end-to-end slice.

Follow the [producer contract](../tom-autodev/references/producer-contract.md), [task-dag schema](../tom-autodev/schemas/task-dag.schema.json) and [phase-specific rules](references/phase-contract.md). Return `task-dag` DraftContent. When filling the DAG half of a merged SPEC job, use that same Spec inside the existing bundle; do not request a new job or gate.

## Decomposition Rules

1. Start from Spec behaviors and acceptance criteria, not repository names.
2. Group the smallest complete path from input through the existing external boundary to the product assertion.
3. Put compatibility or migration behavior in the same slice when it is required to keep the boundary safe.
4. Add a dependency only when the predecessor supplies a real interface, data contract, environment ability, or migration step.
5. For broad refactors use expand-contract: add compatible interface, migrate callers, then remove the old path in a later slice.
6. Name each task by its observable capability, not “implement repository X”, “write tests” or “run regression”. A multi-module migration can use expand-contract slices only when each intermediate state has an honest verification and rollback boundary.

## Validation and Gate

Validate that the DAG is acyclic, edges reference unique existing nodes, every criterion is covered, each module is registered, and every blocker represents a real dependency. Each task needs test IDs, fixtures, expected evidence and a completion predicate. The controller decides completion from actual evidence and requests G3 only when the action requires it; the model neither marks planned tasks passed nor opens an extra gate.

Do not choose file-level implementation details, generate code, run project tests, or call iCode/iPipe in this phase.

**REQUIRED PARENT:** Return the DAG and coverage report to `tom-autodev`; preserve the Spec and input hashes.
