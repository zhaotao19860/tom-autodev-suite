---
name: tom-grill
description: Use when an iCafe requirement is ambiguous, carries no acceptance criteria yet, spans multiple repositories, contains unresolved tradeoffs, or needs a verified decision map before Spec.
---

# Tom Grill

## Overview

Close only the human decisions that block a reliable Spec, and record the acceptance criteria the requirement owner commits to. Facts belong to iCafe, code, project docs, GitNexus, or approved knowledge sources; do not ask the user for facts that can be verified. Acceptance criteria are not facts: they are intent that only the requirement owner can state, so asking for them is in scope.

## Inputs and Outputs

Consume an immutable `RequirementSnapshot`, repository/project evidence, and an optional `DecisionMap`. Produce exactly one result:

- `CLARIFIED`: every blocking decision has a Decision Log entry with question, options, decision, decision maker, evidence references, and input revision.
- `NO_OPEN_DECISIONS`: facts are sufficient and no artificial question was asked.

Also produce a glossary delta and an ADR candidate only when a decision is hard to reverse, surprising to future readers, and has a real alternative.

A run may start from a plain card whose `acceptance` list is empty. Record every criterion agreed with the requirement owner in `acceptance_delta` as `{id, statement, decided_by, evidence}`. Never rewrite the snapshot; the parent unions it with the delta before Spec. Either result may carry an `acceptance_delta`, and collecting acceptance criteria is not itself a decision.

Follow [`references/phase-contract.md`](references/phase-contract.md) and the parent [`tom-autodev`](../tom-autodev/references/phase-protocol.md). Return an ArtifactEnvelope to the parent; never call external adapters directly.

## Procedure

1. Read the requirement, acceptance points, attachments, current code, project rules, and knowledge references.
2. Record a Decision Map only when scope, repository boundaries, test interface, environment ability, or release boundary is unclear.
3. Verify facts independently. Add each unresolved human tradeoff to a queue ordered by dependency.
4. Ask one question at a time. State the decision needed, the known options, the evidence already checked, and the consequence of each option. Wait for a decision before asking the next question.
5. If the snapshot has no acceptance criteria, ask the requirement owner for them the same way: one criterion at a time, each stated so that a test or a named observation point can decide it. Reject a criterion that needs source reading to judge; that belongs in Spec evidence. When the project pins a KU requirement directory, `tom-autodev acceptance-candidates <project>` returns the criteria already written there as a starting list; treat them as unconfirmed drafts, put them to the owner one at a time, and carry the returned `source` into each accepted entry's `evidence`. Never promote a candidate the owner did not confirm.
6. If no decision is required, return `NO_OPEN_DECISIONS`; do not invent a question to make the phase look complete.
7. After each answer, update the Decision Log and invalidate downstream context whose input hash changed.

## Stop Conditions

Stop with `DECISION_REQUIRED` when a tradeoff lacks a decision. Stop with `ACCEPTANCE_REQUIRED` when snapshot `acceptance` and `acceptance_delta` are both empty. Stop with `REQUIREMENT_CHANGED` when the iCafe content hash changes. Do not create tasks, code, tests, or a release plan in this phase.

**REQUIRED PARENT:** Return the result and evidence references to `tom-autodev`; do not call iCode or iPipe directly.
