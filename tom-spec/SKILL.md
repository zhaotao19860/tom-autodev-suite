---
name: tom-spec
description: Use when clarified iCafe requirements need a versioned behavioral Spec, test interfaces, environment requirements, and acceptance traceability before task decomposition.
---

# Tom Spec

## Overview

Convert closed requirement decisions into a reviewable behavioral contract. Specify what must be observable and verifiable without embedding implementation code or volatile line-level plans.

## Required Inputs

Require a `RequirementSnapshot`, `CLARIFIED` or `NO_OPEN_DECISIONS` result, Decision Log, project/language rules, repository evidence, independent product-test repository contract, and current iPipe environment profile. Return `DECISION_REQUIRED` if any behavior or human tradeoff remains open.

Follow the [producer contract](../tom-autodev/references/producer-contract.md), [spec schema](../tom-autodev/schemas/spec.schema.json) and [phase-specific rules](references/phase-contract.md). Return versioned `spec` DraftContent. In a worker-requested merged SPEC job, also apply [tom-tasks](../tom-tasks/SKILL.md) to this same Spec and return one `{spec, dag}` bundle; this is one producer turn, not a recursive workflow.

## Spec Contract

Cover these topics using the schema's actual fields; keep narrative in supported text/evidence fields rather than inventing JSON properties:

1. Problem Statement and Solution.
2. Numbered user/system behaviors, including normal, boundary, exceptional, and compatibility behavior.
3. Implementation decisions at module/interface level and any approved ADR references.
4. Test decisions: selected external test interface, existing precedent, independent expected-result source, fixtures/data, and product-test repository location.
5. Environment Requirements: runner OS/arch, image digest, compiler/SDK/tool versions, hardware/simulator, data, services, capacity, and pipeline config revision.
6. Out of Scope, risks, rollback boundary, and release evidence requirements.
7. Traceability: each iCafe acceptance criterion maps to one or more Spec behaviors and planned iPipe evidence.

Prefer the highest stable existing test interface that observes the target behavior. Add a new interface only when existing interfaces cannot express the requirement, and record why. Use [behavior and test design](references/behavior-and-tests.md) for state, boundary, failure and expected-result rules.

## Completion Gate

Before G2 approval, verify:

- Every acceptance criterion has one unambiguous Spec mapping.
- Every behavior has a product-test case or an explicit human-verification reason.
- Business and independent test repositories describe the same behavior version.
- Environment Requirements are sufficient to decide whether an iPipe runner is eligible.
- Scope, exception, compatibility, and rollback statements do not conflict.
- The Spec has a monotonic `version` (the content schema's field); the controller computes content identity from the actual content.

Stop on missing evidence instead of filling gaps with assumptions. Create a DAG only for an explicitly merged job; otherwise leave it to TASKS. Do not edit code, run project tests, call iCode/iPipe or create extra gates.

**REQUIRED PARENT:** Return the Spec artifact and evidence references to `tom-autodev` for G2 approval.
