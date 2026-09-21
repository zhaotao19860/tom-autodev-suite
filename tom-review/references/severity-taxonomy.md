# Severity, Disposition, and Blocking

These are separate decisions. Use the enum values in `review.schema.json`, not HIGH/MEDIUM/BLOCK.

| Severity | Observable impact when triggered | Default for a confirmed defect |
|---|---|---|
| `P0` | Active or immediately reachable catastrophic data loss, widespread outage, or critical compromise | Blocking |
| `P1` | Required capability broken, credible security compromise, unrecoverable operation, or major compatibility/data-integrity failure | Blocking |
| `P2` | Bounded but reproducible behavior/contract defect under a specific condition | Blocking when an approved acceptance point or mandatory project rule is violated; otherwise explain why it is non-blocking |
| `P3` | Low-impact maintenance concern without required-behavior failure | Do not raise unsolicited style findings; a provider suggestion may be disposed as non-blocking |

Choose severity from the demonstrated consequence and reachable trigger. A rule catalog priority is a starting point, not evidence. State a concrete reason for a `P2` blocking choice in `evidence`; do not automatically let all medium-impact defects pass.

| Classification | Meaning | `blocking` |
|---|---|---|
| `CONFIRMED` | Evidence establishes a defect or applicable mandatory-rule violation | Per impact and contract above |
| `REJECTED_WITH_REASON` | Refuted, outside the approved scope, or merely optional advice without an established defect | `false`; explain which reason applies |
| `NEEDS_CLARIFICATION` | A specific missing answer prevents judging required behavior or a material risk | `false`, but the parent **still stops** for clarification |

For an unresolved material question, severity describes the consequence being investigated, not a confirmed exploit. `disposition_reason` names the missing fact and evidence needed. Never emit `ACCEPT` while any `NEEDS_CLARIFICATION` remains. A provider that cannot inspect the required scope is `INCOMPLETE`, not a list of invented defects.

## Metrics and preferences

Class length, parameter count, nesting, coupling, duplication, a single-implementation interface, or direct client construction are investigation cues. They do not establish a defect or approval block by themselves. Check the pinned project policy and concrete effect first. Optional refactoring advice does not become a clarification question merely because the author has not justified it.

Do not require interfaces, retries, or abstractions absent a project contract or demonstrated failure. Retrying a non-idempotent operation can introduce a defect. A known approved design is not overturned by a generic checklist threshold.
