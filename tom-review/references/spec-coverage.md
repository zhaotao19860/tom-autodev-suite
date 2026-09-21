# Acceptance Coverage Review

Start from the approved task's acceptance points as well as the diff. A required behavior can be missing without adding an incorrect line.

## Working matrix

For each AC owned by the current task, record:

| AC and Spec reference | Responsible module/interface | Status | Implementation + product-test evidence | Checked scope/limitations |
|---|---|---|---|---|
| Exact approved ID and clause | From Task Plan/DAG | `implemented`, `missing`, `deviated`, `partial`, or `unknown` | Pinned repository/revision and relevant symbol/test ID | Include affected callers and delegations |

This matrix is review evidence, not a new top-level DraftContent field. Retain it with the review's actual supporting evidence and reference relevant rows in finding `evidence`. Do not invent artifact IDs, uploaded URLs, test execution receipts, or extra schema fields to hold it.

- `implemented`: code or the relevant deliverable establishes the required behavior. Check independent test assertions when the Task Plan requires them; reading tests does not establish that they passed. Record implementation and test coverage separately: a missing test is not proof that the behavior is absent.
- `missing`: the task owns an explicit requirement, the responsible path and its delegates were inspected, and the required behavior is absent.
- `deviated` / `partial`: implementation contradicts the contract or covers only some required branches; give the specific counterexample.
- `unknown`: missing input, incomplete scope, or an ambiguous contract prevents judgment. Explain which applies.

Inspect existing baseline code and delegated implementations before declaring an omission. An absent diff keyword or absent newly added test is insufficient: existing implementation/tests may already cover the behavior. Do not require a change for an AC owned by another DAG task; verify the recorded boundary/dependency instead.

## Findings for omissions

Use `axis: spec`, the exact `acceptance_point_ids`, and an honest `location` such as `Spec v3 / AC-4; module session; cancel interface`. If an existing dispatch/interface is the omission boundary, cite its real location too. Never invent a new-code line for code that does not exist.

The `evidence` string states the approved behavior, complete inspected scope, trigger, expected result, actual missing/partial behavior, repair direction, and a regression scenario. A missing implementation proven this way is `CONFIRMED`; assign impact and blocking by [`severity-taxonomy.md`](severity-taxonomy.md).

If required code/Spec could not be read or the search was truncated, mark the affected axis incomplete and return `INCOMPLETE`; do not upgrade `unknown` to `missing`. If inspection is complete but a specific answer is needed to judge required behavior, use `NEEDS_CLARIFICATION` and `REJECT` as defined in [`phase-contract.md`](phase-contract.md). Optional advice is not an unknown AC.

For ordinary documentation/comment changes, this check may be short: verify the task's changed claims and contract consistency. Commands, configuration, public API promises, and agent instructions require their relevant behavior checks even when stored in Markdown.
