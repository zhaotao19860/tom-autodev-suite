---
name: tom-review
description: Use when a fixed-baseline Change Set needs independent Standards and Spec review before iCode submission or after a repair.
---

# Tom Review

## Inputs

Require the approved Spec and Task Plan, Change Set, business/test repository revisions, Review Baseline, language/project rules, knowledge and impact references, and matching input hashes. Review the exact diff, not an approximate current workspace.

Follow [`references/phase-contract.md`](references/phase-contract.md). Emit a `review` ArtifactEnvelope with both reports and receipts; the parent alone performs G7/iCode gating.

## Two Axes

Run independent reports:

- **Standards:** repository and language/project conventions, affected callers, naming, duplication, responsibility, unsupported abstraction, scope, and tests that assert external behavior.
- **Spec:** required behavior, exceptions, compatibility, test interface, acceptance coverage, iPipe plan, and business/test semantic alignment.

Every finding contains axis, severity, repository, path/symbol/line, evidence reference, affected acceptance criterion, and blocking flag.

## Finding Reception

Classify every provider suggestion exactly once:

- `CONFIRMED`: current code, Spec, rules, and impact evidence establish the issue.
- `REJECTED_WITH_REASON`: evidence shows it is inapplicable or conflicts with an approved decision; record the technical reason.
- `NEEDS_CLARIFICATION`: Spec, revision, location, impact, or reproduction evidence is insufficient; pause the run and request a decision.

Do not use `pending`, `unverified`, or `incomplete` as finding states. `INCOMPLETE` is reserved for a Review provider that is unavailable, times out, lacks scope, or lacks a Spec.

## Verdict and Gate

Return `PASS` only when both axes pass and no blocking `CONFIRMED` finding remains. The Review result carries no approval. Return `FAIL` for a blocking confirmed finding. Return `INCOMPLETE` for provider capability gaps. Any `NEEDS_CLARIFICATION`, stale/hash-mismatched baseline, missing input, or other non-PASS result stops parent submission. Only `PASS` may allow the parent to request a separately hash-bound G7 iCode approval for this exact Change Set; confirmed blocking findings return to `tom-diagnose`, never directly to code edits.

Do not modify code, run project compile/tests, call iCode/iPipe, or silently accept a suggestion in this phase.

**REQUIRED PARENT:** Return both reports, finding classifications, verdict, baseline, and input hash to `tom-autodev`.
