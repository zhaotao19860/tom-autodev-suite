---
name: tom-plan
description: Use when an approved frontier task needs a concrete per-task implementation plan before any business or product-test code is generated.
---

# Tom Plan

## Overview

Turn one approved Task DAG node into an executable plan another agent can follow without rediscovering architecture. Resolve plan ambiguity before G4; do not defer missing decisions to implementation.

## Inputs and Output

Consume the approved Spec, one frontier task, WorkspaceGate evidence, language/project rules, knowledge and impact references, test-repository contract, and stable iPipe profile. Follow the [producer contract](../tom-autodev/references/producer-contract.md), [task-plan schema](../tom-autodev/schemas/task-plan.schema.json) and [phase-specific rules](references/phase-contract.md). Return `task-plan` DraftContent containing:

- exact repositories and files/modules/symbols to create or modify;
- consumed interfaces and interfaces produced for later tasks;
- ordered business-repository and product-test-repository changes;
- valid, boundary, invalid, compatibility and regression test IDs, fixtures, assertions, and expected results;
- iPipe checkout, build, unit, regression, integration, environment and evidence parameters;
- knowledge queries, blast-radius scope, Review scope, completion predicate, rollback, and excluded work;
- ordered checklist items; encode the precondition, action and observable completion check inside the schema's `item` text. A checklist is not a persisted execution log.

Use concrete names from pinned revisions. Copy `g4_input_hash` from the action and preserve the DAG's acceptance IDs, test IDs, fixtures and target module. Apply [behavior and test design](../tom-spec/references/behavior-and-tests.md) to assertions. For risky edits name the intermediate compatibility state and how implementation can recognize a completed step after interruption. If the plan conflicts with Spec, report `SPEC_CONFLICT` to the parent instead of reinterpreting it.

## Gate

Before handoff, verify every criterion/task field is covered, each step is checkable, and business/test changes form one Change Set without local project execution. The parent applies the current action's G4/mode; do not request a redundant gate or start implementation yourself.

Do not generate code, modify repositories, run project tests, call iCode/iPipe, or create a second task in this phase.

**REQUIRED PARENT:** Return the `Task Plan`, input hash, and evidence references to `tom-autodev` for G4.
