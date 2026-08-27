---
name: tom-plan
description: Use when an approved frontier task needs a concrete per-task implementation plan before any business or product-test code is generated.
---

# Tom Plan

## Overview

Turn one approved Task DAG node into an executable plan another agent can follow without rediscovering architecture. Resolve plan ambiguity before G4; do not defer missing decisions to implementation.

## Inputs and Output

Consume the approved Spec, one frontier task, WorkspaceGate evidence, language/project rules, knowledge and impact references, test-repository contract, and stable iPipe profile. Produce a versioned `Task Plan` containing:

Follow [`references/phase-contract.md`](references/phase-contract.md). Return a `task-plan` ArtifactEnvelope to the parent for KU persistence, iCafe linking, and G4 approval.

- exact repositories and files/modules/symbols to create or modify;
- consumed interfaces and interfaces produced for later tasks;
- ordered business-repository and product-test-repository changes;
- valid, boundary, invalid, compatibility and regression test IDs, fixtures, assertions, and expected results;
- iPipe checkout, build, unit, regression, integration, environment and evidence parameters;
- knowledge queries, blast-radius scope, Review scope, completion predicate, rollback, and excluded work;
- checkbox steps with a precondition, one action, and observable evidence.

Use concrete names from current revisions. Do not write “appropriate handling”, “add tests”, “later”, or an undefined type/function. If the plan conflicts with Spec, return `SPEC_CONFLICT` to `tom-spec`; do not reinterpret the conflict.

## Gate

Before G4, verify every acceptance criterion and task field is covered, every step is independently checkable, business and test changes form one Change Set, and no step invokes local project compilation or tests. Wait for human approval before `tom-implement` consumes the plan.

Do not generate code, modify repositories, run project tests, call iCode/iPipe, or create a second task in this phase.

**REQUIRED PARENT:** Return the `Task Plan`, input hash, and evidence references to `tom-autodev` for G4.
