# Failure Taxonomy

| Reason | Repair allowed | Route |
|---|---:|---|
| `ACCEPTANCE_CRITERIA_MISSING` | no | Return to `tom-grill` and agree criteria with the requirement owner. |
| `ACCEPTANCE_REQUIRED` | no | `tom-grill` stopped itself because the union would stay empty; agree criteria with the requirement owner. |
| `ACCEPTANCE_DELTA_CONFLICT` | no | Return to `tom-grill`; a delta id must not redefine a snapshot criterion. |
| `APPROVAL_CONTENT_INVALID` | yes | `request-approval --content` could not be read or parsed; pass the phase envelope JSON. |
| `APPROVAL_CONTENT_MISMATCH` | yes | The summarized content does not match the gate's bound hashes; open the gate on the envelope actually being approved. |
| `APPROVAL_DELIVERY_FAILED` | yes | No channel took the card and nothing was sent; fix the client defect and ask again. |
| `APPROVAL_DELIVERY_RETRYABLE` | yes | `reissue-approval` refused because a plain retry re-posts only the channel that failed; do that instead of burning a new action name. |
| `LOCAL_EXECUTION_FORBIDDEN` | no | Refuse local project build/test and create an iPipe validation plan. |
| `REVIEW_FAILED` | after diagnosis | Confirm finding, then diagnose. |
| `REVIEW_INCOMPLETE` | no | Use human Review fallback. |
| `REVIEW_NEEDS_CLARIFICATION` | no | A finding is classified `NEEDS_CLARIFICATION`: get the answer from the requirement owner or the reviewer, then re-review. Not a diagnosis — there is no established defect to root-cause yet. |
| `CODE_FAILURE` | after diagnosis | Diagnose, revise Spec/Task Plan, generate patch. |
| `TEST_FAILURE` | after diagnosis | Distinguish product, test, and environment defects. |
| `BASELINE_UNVERIFIED` | no | Require human baseline decision. |
| `DIAGNOSIS_INCOMPLETE` | no | Gather evidence or request an approved iPipe diagnostic stage. |
| `ENV_UNSATISFIED` | no | Restore environment; keep business code unchanged. |
| `ENV_TRANSIENT` | no | Perform read-only health rechecks. |
| `PIPELINE_TRANSIENT` | no code repair | Retry read-only queries; G8 controls a stage rerun. |
| `REVISION_MISMATCH` | no | Stop and discard the result. |

Use one root-cause hypothesis per repair round. Stop automatic proposals after two rounds with the same signature and no progress. Force an architecture Review after three unsuccessful approved fixes. Stop after five repair rounds for a task.

## External writes: three outcomes, not two

Every failure around an external write (KU, iCafe, an approval channel, iCode, iPipe) is one of three things, and the same four bugs kept appearing because the first two were treated as one.

| Class | Meaning | Reason codes | What may happen next |
|---|---|---|---|
| Nothing was sent | The client raised before dispatching: a missing method, a signature that does not match, a module that will not import. A defect in this process, so it is not evidence about the remote side. | `APPROVAL_DELIVERY_FAILED` | Withdraw the claim and retry after fixing the defect. Retrying cannot double-write. |
| Outcome unknown | The call left and no usable answer came back: a socket dying mid-request, a timeout, a malformed response. | `QUERY_REQUIRED`, `RECOVERY_REQUIRED` | Reconcile by asking the remote side. Never retry blind. Only the narrow, provable case above may withdraw a claim. |
| Redrivable | The write is keyed on its own content, so calling the same operation again re-attempts exactly the write that is open. | any pending intent in `_PUBLISH_REDRIVEN_OPERATIONS` | Call the phase again; it settles its own intent either way. |

Two rules follow, and both were learned the hard way:

- `retry_allowed` states which class this is, not how the caller feels about waiting. `false` means the outcome is unknown and must be reconciled; `true` means nothing was sent.
- A pending intent is not automatically a reason to stop. Stop when no further phase work can settle it. Treating every pending intent as terminal parked the x86bgw CDN-URL run nine times over a KU write that had already succeeded, because the reconciliation lived inside the very call the guard was refusing.

### When the outcome will never be known

An `outcome unknown` intent that nobody can reconcile — the bot is gone, the build was garbage-collected, the person who could look has moved on — still holds the run. `tom-autodev abandon-intent <intent_id> --reason R --actor A` releases it by *writing* an abandonment receipt (`ok: false`, `INTENT_ABANDONED`, carrying who decided and why), never by deleting the intent. The row therefore stays in `external_results`, in `trace`, and in the run summary's failure groups, where `abandoned_intent_count` keeps it out of `external_receipt_count` so G10 cannot read a hand-cut knot as a clean external write.

It is deliberately not approval-gated: the intents that strand a run are frequently the approval deliveries themselves, and a gate that needs the stuck channel to open it is not an escape hatch. `reason` and `actor` are required in its place. A real outcome arriving afterwards fails loudly with `RECEIPT_CONFLICT` rather than being absorbed.

`withdraw_intent` remains the right call only for the narrow `nothing was sent` case, where the write provably never happened and there is nothing to account for.
