---
name: tom-review
description: Use when a fixed-baseline Change Set needs independent Standards and Spec review before iCode submission or after a repair.
---

# Tom Review

## Suite Maintenance Review

When the user is reviewing **tom-autodev-suite itself**, first read
[`../docs/WORKFLOW_EXIT_CRITERIA.md`](../docs/WORKFLOW_EXIT_CRITERIA.md) from the real
suite directory (resolve an installed skill symlink if needed). Use its fixed scope,
evidence requirements, report template and stop/reopen rules. This mode produces a
suite maintenance report, not a business `review` DraftContent, and does not start a run.
The remaining sections apply to business Change Set review; keep that runtime contract unchanged.

## Inputs

Read the approved Spec/Task Plan, Change Set, Review Baseline, pinned business/test revisions, language/project rules, and impact evidence. Review the exact diff against matching inputs.

Use [scope collection](references/scope-collection.md) when the input lacks a complete per-repository file inventory. The bundled helper reads fixed commits without checkout or project execution; its output is supporting evidence, not a review verdict.

Follow [`references/phase-contract.md`](references/phase-contract.md). Return `review` **DraftContent** for the current ProducerJob through `submit-draft`. The worker owns the envelope, hashes, receipts, and completion; the parent owns G7/iCode gating.

## Two Axes

Assess both axes separately in the same draft; two axes do not require two model calls:

- **Standards:** repository and language/project conventions, affected callers, naming, duplication, responsibility, unsupported abstraction, scope, and tests that assert external behavior.
- **Spec:** required behavior, exceptions, compatibility, test interface, acceptance coverage, iPipe plan, and business/test semantic alignment.

Use the output slots in `phase-contract.md`. Put repository/revision and actual evidence references in `location`/`evidence`; never invent code lines for omissions.

## Review Method

Review is source-only: never run project code, compile, or test. Load by the changed behavior:

- [`references/spec-coverage.md`](references/spec-coverage.md): **always.** Check the current task's acceptance points, including required behavior with no new code line.
- [`references/review-heuristics.md`](references/review-heuristics.md): for changed executable behavior and affected callers; select the relevant data-flow, boundary, recovery, consistency, and contract passes.
- [`references/security-checklist.md`](references/security-checklist.md): when the change touches web/HTTP, auth, crypto, deserialization, or external input. Feeds Standards.
- [`references/rule-catalog.md`](references/rule-catalog.md): read the rule families triggered by the changed language and behavior; a reading checklist is not scanner execution evidence.
- [`references/severity-taxonomy.md`](references/severity-taxonomy.md) and [`references/false-positive-suppression.md`](references/false-positive-suppression.md): when disposing findings.

For ordinary prose/comments, check changed claims and references. Expand for executable commands, configuration, public behavior, or agent instructions. Keep both axes; skip unrelated rule families.

## Finding Reception

Use evidence appropriate to the defect type: injection needs source-to-sink tracing; secrets/configuration and logic defects have different requirements. Emit `P0`–`P3`, independently of classification and blocking. Metric thresholds alone are not defects.

Classify every provider suggestion exactly once, in the finding's required `classification` field:

- `CONFIRMED`: current code, Spec, rules, and impact evidence establish the issue.
- `REJECTED_WITH_REASON`: refuted, outside this task, or optional advice without a defect; give the precise `disposition_reason`, with `blocking: false`.
- `NEEDS_CLARIFICATION`: an answer is needed to judge required behavior/material risk; give the missing fact and owner in `disposition_reason`, with `blocking: false`. **This still stops the run** with `REVIEW_NEEDS_CLARIFICATION`. Optional refactoring advice does not qualify.

`disposition_reason` is required for both non-confirmed values. Do not invent classification values. Provider/input/scope gaps are Review `INCOMPLETE`.

## Verdict and Gate

Use only `ACCEPT`, `REJECT`, or `INCOMPLETE`, following the contract's decision table. `ACCEPT` requires complete axes, no blocker, and no `NEEDS_CLARIFICATION`; only it permits the parent to request hash-bound G7. Review carries no approval. Confirmed blockers route through `tom-diagnose`; unanswered questions return to their owner.

Do not modify code, run project compile/tests, call iCode/iPipe, or silently accept a suggestion in this phase.

**REQUIRED PARENT:** Return the Review DraftContent and its actual supporting evidence to `tom-autodev`; the worker owns phase completion.
