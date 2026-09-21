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

Every finding contains axis, severity, repository, path/symbol/line, evidence reference, affected acceptance criterion, blocking flag, and `classification`.

## Review Method

Pure-prompt heuristics, read-only — never compile or run. Load:

- [`references/review-heuristics.md`](references/review-heuristics.md): **always.** Data-flow/boundary/adversarial/variant/recovery/consistency/contract passes and the D-01..D-07 semantic defects, each tagged to an axis.
- [`references/security-checklist.md`](references/security-checklist.md): when the change touches web/HTTP, auth, crypto, deserialization, or external input. Feeds Standards.
- [`references/rule-catalog.md`](references/rule-catalog.md): rule-ID + severity read-checks (G-SECRET/EXCEPT/INPUT/SEC/DB/PERF/LOG/ARCH, BIZ-*).
- [`references/severity-taxonomy.md`](references/severity-taxonomy.md) and [`references/false-positive-suppression.md`](references/false-positive-suppression.md): when disposing findings.

## Finding Reception

Run every candidate finding through the sink-first FP firewall (`false-positive-suppression.md`) before recording it: prefer a missed report over a false one. Map its triage to `classification` — reject → `REJECTED_WITH_REASON`, 待确认/unknown → `NEEDS_CLARIFICATION`, confirmed → `CONFIRMED`. Do not import raw HIGH/MEDIUM/BLOCK verdict words into the artifact; severity lives in `severity`/`blocking`, disposition lives in `classification`.

Classify every provider suggestion exactly once, in the finding's required `classification` field:

- `CONFIRMED`: current code, Spec, rules, and impact evidence establish the issue.
- `REJECTED_WITH_REASON`: evidence shows it is inapplicable or conflicts with an approved decision; record the technical reason in `disposition_reason`, and do not mark it blocking — a suggestion you just refuted cannot hold the run.
- `NEEDS_CLARIFICATION`: Spec, revision, location, impact, or reproduction evidence is insufficient; state what is missing in `disposition_reason`. The run stops with `REVIEW_NEEDS_CLARIFICATION` rather than going to `tom-diagnose`, which root-causes failures and cannot answer a question.

`disposition_reason` is required for both non-confirmed values, and an `ACCEPT` verdict over a `NEEDS_CLARIFICATION` finding is rejected as inconsistent: the schema will not let the artifact claim the code is clean and unexamined at once.

Do not use `pending`, `unverified`, or `incomplete` as finding states. `INCOMPLETE` is reserved for a Review provider that is unavailable, times out, lacks scope, or lacks a Spec.

## Verdict and Gate

Return `PASS` only when both axes pass and no blocking `CONFIRMED` finding remains. The Review result carries no approval. Return `FAIL` for a blocking confirmed finding. Return `INCOMPLETE` for provider capability gaps. Any `NEEDS_CLARIFICATION`, stale/hash-mismatched baseline, missing input, or other non-PASS result stops parent submission. Only `PASS` may allow the parent to request a separately hash-bound G7 iCode approval for this exact Change Set; confirmed blocking findings return to `tom-diagnose`, never directly to code edits.

Do not modify code, run project compile/tests, call iCode/iPipe, or silently accept a suggestion in this phase.

**REQUIRED PARENT:** Return both reports, finding classifications, verdict, baseline, and input hash to `tom-autodev`.
