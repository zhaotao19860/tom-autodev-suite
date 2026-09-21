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

Follow the [producer contract](../tom-autodev/references/producer-contract.md) and [decision-log schema](../tom-autodev/schemas/decision-log.schema.json). Return `decision-log` DraftContent; the parent creates the envelope, receipts and applicable gate. See [phase-specific rules](references/phase-contract.md).

## Procedure

1. Read the requirement, acceptance points, attachments, current code, project rules, and knowledge references.
2. Record a Decision Map only when scope, repository boundaries, test interface, environment ability, or release boundary is unclear.
3. Verify facts independently. Add each unresolved human tradeoff to a queue ordered by dependency.
4. Ask only unresolved blocking questions, ordered by dependency. Group closely related choices into one concise request when the owner can decide them together. Include checked evidence and the consequence of each option. Do not ask again for a decision explicit in the request or approved input.
5. If acceptance criteria are missing, propose observable criteria grounded in the request and ask the owner to confirm or amend the group. Existing owner-supplied criteria are already intent, not a reason to repeat confirmation. Each criterion needs a test or named observation point; source-reading details belong in Spec evidence. When the parent supplies `acceptance-candidates` from a pinned KU directory, treat them as unconfirmed drafts and carry each accepted candidate's source into its evidence. Never silently promote inferred criteria.
6. If no decision is required, return `NO_OPEN_DECISIONS`; do not invent a question to make the phase look complete.
7. After each answer, return the updated Decision Log and identify changed assumptions; the controller invalidates affected downstream context. Do not mutate the original snapshot or state ledger.

## Stop Conditions

Stop with `DECISION_REQUIRED` when a tradeoff lacks a decision. Stop with `ACCEPTANCE_REQUIRED` when snapshot `acceptance` and `acceptance_delta` are both empty. Stop with `REQUIREMENT_CHANGED` when the iCafe content hash changes. Do not create tasks, code, tests, or a release plan in this phase.

**REQUIRED PARENT:** Return the result and evidence references to `tom-autodev`; do not call iCode or iPipe directly.
