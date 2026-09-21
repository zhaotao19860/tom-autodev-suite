# Severity Taxonomy

Bind "subjective code smell" to hard thresholds so a finding is raised only when it crosses a line — this keeps convention judgments from becoming noise. Judge by reading; tom-review runs no metric scripts, so treat the thresholds below as reading guides, not tool output.

Re-express every crossing into tom-review's `severity` + `blocking` + `classification`:

- **Red flag** — deterministic, crosses a hard threshold → high `severity`, `blocking: true`, `classification: CONFIRMED` (evidence must establish the crossing).
- **Yellow flag** — advisory, context-dependent → `severity: MEDIUM`, `blocking: false`, `classification: NEEDS_CLARIFICATION` with the missing context recorded in `disposition_reason`. Never let a yellow flag hold the run.

## Red flags (blocking + CONFIRMED)

| Item | Threshold | Axis / note |
|------|-----------|-------------|
| God class | single class > 1000 lines | Standards (responsibility) |
| Circular dependency | A→B→C→A | Standards; needs whole-graph context — confirm before blocking |
| Domain layer depends on framework | inner package imports a concrete infrastructure class | Standards (arch layer, see G-ARCH-001 in [`rule-catalog.md`](rule-catalog.md)) |
| External service call with no interface | directly `new`-ing a concrete HTTP/DB client | Standards (unsupported abstraction) |
| Hardcoded secret | AK/SK / token / password literal | Standards (see G-SECRET-001) |

## Yellow flags (non-blocking MEDIUM + NEEDS_CLARIFICATION)

| Item | Threshold |
|------|-----------|
| Class coupling (CBO) | > 10 |
| Function parameters | > 5 |
| Nesting depth | > 4 levels |
| Duplicated block | > 10 lines |
| Single-implementation interface | an interface with only one impl (over-abstraction) |

A yellow flag is a prompt to confirm intent with the parent, not a defect on its own. If reading confirms it is intentional and justified, dispose it `REJECTED_WITH_REASON`; if it cannot be judged from the baseline, keep it `NEEDS_CLARIFICATION`.

> When a heuristic in [`review-heuristics.md`](review-heuristics.md) marks its own severity (e.g. D-01 HIGH), that mark wins. This table is the fallback only for items with no explicit mark.
