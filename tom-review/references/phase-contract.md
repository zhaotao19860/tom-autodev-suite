# Review Phase Contract

The authoritative content shape is [`review.schema.json`](../../tom-autodev/schemas/review.schema.json). Follow the shared [`producer-contract.md`](../../tom-autodev/references/producer-contract.md) for pinned inputs, DraftContent submission, worker-owned envelopes/receipts, and gates. This file specifies Review's output and verdict rules.

## Output slots

Copy identities from the pinned action/Change Set; never derive them from the latest workspace. Return only Review DraftContent for the matching ProducerJob.

| Field | Required content |
|---|---|
| `task_id` | Current task identity |
| `change_set_hash` | Exact reviewed candidate hash supplied by the pinned input |
| `baseline_revisions` | `business` and `tests`, both from that input |
| `axes.standards`, `axes.spec` | Each has `complete` and `finding_ids`; IDs exactly match findings assigned to that axis |
| `findings` | Array of classified findings; `[]` is valid after complete review |
| `verdict` | `ACCEPT`, `REJECT`, or `INCOMPLETE` |
| `provider` | Actual `kind` and `identity`; do not invent a model/version |
| `completeness_state` | `COMPLETE` or `INCOMPLETE` |

A finding has exactly `id`, `axis` (`standards`/`spec`), `severity` (`P0`–`P3`), `location`, `evidence`, `acceptance_point_ids`, `blocking`, and `classification`; add `disposition_reason` for both non-confirmed classifications. IDs are unique. Use an empty acceptance array for a general standards defect with no related AC instead of fabricating one. Include trigger, expected/actual effect, evidence references, and a concrete repair/verification direction in the evidence string. No extra `repository`, `line`, `coverage`, or `reports` fields are accepted.

This is a shape example with illustrative identities, not evidence to reuse:

```json
{
  "task_id": "T1",
  "change_set_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "baseline_revisions": {"business": "business-revision-example", "tests": "test-revision-example"},
  "axes": {
    "standards": {"complete": true, "finding_ids": []},
    "spec": {"complete": true, "finding_ids": ["R1"]}
  },
  "findings": [{
    "id": "R1",
    "axis": "spec",
    "severity": "P1",
    "location": "Spec v3 / AC-2; business module session; cancel operation",
    "evidence": "AC-2 requires cancellation to prevent a second charge. The reviewed session handler retries the charge after cancellation. Trigger: cancel after the first charge acknowledgement, before retry dispatch. Expected: no retry; actual: a second charge. Review evidence: pinned session handler and caller branches. Add cancellation checking at dispatch and a regression for this ordering.",
    "acceptance_point_ids": ["AC-2"],
    "blocking": true,
    "classification": "CONFIRMED"
  }],
  "verdict": "REJECT",
  "provider": {"kind": "source-only", "identity": "reviewer-example"},
  "completeness_state": "COMPLETE"
}
```

## Decision table

| Observed result | Draft values | Parent result |
|---|---|---|
| Both axes inspected; no blocker or material unanswered question | Both axes complete; `COMPLETE`; `ACCEPT` | May request G7 for the exact Change Set |
| At least one confirmed blocker | `COMPLETE`; `REJECT`; blocker is `CONFIRMED` + `blocking: true` | Routes to diagnosis |
| Required behavior/material risk cannot be judged until a specific question is answered | Inspection `COMPLETE`; `REJECT`; relevant finding is `NEEDS_CLARIFICATION`, `blocking: false`, with reason | Stops with `REVIEW_NEEDS_CLARIFICATION` |
| Required baseline/Spec/code scope unavailable, provider timeout, or review truncated | Affected axis `complete: false`; `INCOMPLETE` completeness and verdict | Stops with `REVIEW_INCOMPLETE` |

If both incomplete review and findings exist, incompleteness takes precedence. Severity never substitutes for classification: do not turn weak evidence into a lower-priority confirmed finding. Resolve baseline drift through the parent before reviewing; a review of different revisions cannot be made valid by selecting another verdict.
