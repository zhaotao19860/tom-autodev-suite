---
name: tom-spec
description: Use when clarified iCafe requirements need a versioned behavioral Spec, test interfaces, environment requirements, and acceptance traceability before task decomposition.
---

# Tom Spec

## Overview

Convert closed requirement decisions into a reviewable behavioral contract. Specify what must be observable and verifiable without embedding implementation code or volatile line-level plans.

## Required Inputs

Require a `RequirementSnapshot`, `CLARIFIED` or `NO_OPEN_DECISIONS` result, Decision Log, project/language rules, repository evidence, independent product-test repository contract, and current iPipe environment profile. Return `DECISION_REQUIRED` if any behavior or human tradeoff remains open.

Follow [`references/phase-contract.md`](references/phase-contract.md). Emit the versioned `spec` ArtifactEnvelope, publish through the parent KnowledgeSync boundary, and include the iCafe comment receipt in the envelope.

## Spec Contract

Produce these sections in order:

1. Problem Statement and Solution.
2. Numbered user/system behaviors, including normal, boundary, exceptional, and compatibility behavior.
3. Implementation decisions at module/interface level and any approved ADR references.
4. Test decisions: selected external test interface, existing precedent, independent expected-result source, fixtures/data, and product-test repository location.
5. Environment Requirements: runner OS/arch, image digest, compiler/SDK/tool versions, hardware/simulator, data, services, capacity, and pipeline config revision.
6. Out of Scope, risks, rollback boundary, and release evidence requirements.
7. Traceability: each iCafe acceptance criterion maps to one or more Spec behaviors and planned iPipe evidence.

Prefer the highest stable existing test interface that observes the target behavior. Add a new interface only when existing interfaces cannot express the requirement, and record why.

## Completion Gate

Before G2 approval, verify:

- Every acceptance criterion has one unambiguous Spec mapping.
- Every behavior has a product-test case or an explicit human-verification reason.
- Business and independent test repositories describe the same behavior version.
- Environment Requirements are sufficient to decide whether an iPipe runner is eligible.
- Scope, exception, compatibility, and rollback statements do not conflict.
- The Spec has a monotonic `spec_version` and canonical `content_hash`.

Stop on missing evidence instead of filling gaps with assumptions. Do not create the Task DAG, edit code, run project tests, or call iCode/iPipe in this phase.

**REQUIRED PARENT:** Return the Spec artifact and evidence references to `tom-autodev` for G2 approval.
