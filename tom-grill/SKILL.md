---
name: tom-grill
description: Use when an iCafe requirement is ambiguous, spans multiple repositories, contains unresolved tradeoffs, or needs a verified decision map before Spec.
---

# Tom Grill

## Overview

Close only the human decisions that block a reliable Spec. Facts belong to iCafe, code, project docs, GitNexus, or approved knowledge sources; do not ask the user for facts that can be verified.

## Inputs and Outputs

Consume an immutable `RequirementSnapshot`, repository/project evidence, and an optional `DecisionMap`. Produce exactly one result:

- `CLARIFIED`: every blocking decision has a Decision Log entry with question, options, decision, decision maker, evidence references, and input revision.
- `NO_OPEN_DECISIONS`: facts are sufficient and no artificial question was asked.

Also produce a glossary delta and an ADR candidate only when a decision is hard to reverse, surprising to future readers, and has a real alternative.

Follow [`references/phase-contract.md`](references/phase-contract.md) and the parent [`tom-autodev`](../tom-autodev/references/phase-protocol.md). Return an ArtifactEnvelope to the parent; never call external adapters directly.

## Procedure

1. Read the requirement, acceptance points, attachments, current code, project rules, and knowledge references.
2. Record a Decision Map only when scope, repository boundaries, test interface, environment ability, or release boundary is unclear.
3. Verify facts independently. Add each unresolved human tradeoff to a queue ordered by dependency.
4. Ask one question at a time. State the decision needed, the known options, the evidence already checked, and the consequence of each option. Wait for a decision before asking the next question.
5. If no decision is required, return `NO_OPEN_DECISIONS`; do not invent a question to make the phase look complete.
6. After each answer, update the Decision Log and invalidate downstream context whose input hash changed.

## Stop Conditions

Stop with `DECISION_REQUIRED` when a tradeoff lacks a decision. Stop with `REQUIREMENT_CHANGED` when the iCafe content hash changes. Do not create tasks, code, tests, or a release plan in this phase.

**REQUIRED PARENT:** Return the result and evidence references to `tom-autodev`; do not call iCode or iPipe directly.
