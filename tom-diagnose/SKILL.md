---
name: tom-diagnose
description: Use when Review, iPipe, release or product-test evidence needs root-cause diagnosis, or when an explicitly identified internal host needs bounded relay/tmux remote investigation of logs, processes, routing or VIP connectivity.
---

# Tom Diagnose

## Select the context

| Supplied context | Mode and output |
|---|---|
| A run/action or frozen Review/iPipe/release failure bundle | **Workflow diagnosis:** return `diagnosis` DraftContent to the parent under the [producer contract](../tom-autodev/references/producer-contract.md). |
| An explicit host and a request to inspect it, without a workflow job | **Remote investigation:** use the bundled [remote operations](references/remote-operations.md); return a human-readable evidence report. No iCafe run, project registration or fake pipeline IDs are needed. |
| Workflow failure plus missing live-host evidence | Keep workflow mode. Present a bounded evidence request to the parent; use remote operations only for its authorized target/commands. Observations supplement, but never replace, iPipe execution receipts. |

Remote operation does not authorize deployment, an iPipe rerun or code repair. BNS/Matrix/container transports are unsupported by the bundled relay helper; require an exact supported SSH target instead of guessing.

## Diagnose from evidence

Read [root-cause protocol](references/root-cause-protocol.md). Freeze the failure identity, business/test revisions, stage/job, environment and log window; compare with a valid passing baseline. Trace the first broken component boundary, form one falsifiable hypothesis, and specify a minimal discriminating observation. Keep observed facts, inference and unknowns separate.

Read project runtime topology only for the selected project; NPL compile evidence also uses the suite's `tom-lang-npl` diagnostics. Only confirmed Review findings are failure inputs; a question about intended behavior returns to the requirement owner.

Environment, transport, revision and stale-artifact failures do not justify business patches. A flaky or non-reproduced failure needs additional evidence, not repeated speculative edits. Do not edit code in workflow diagnosis; repair implementation belongs to the approved PLAN/IMPLEMENT path.

## Workflow output and limits

Use the action's [diagnosis schema](../tom-autodev/schemas/diagnosis.schema.json) and [phase-specific mapping](references/phase-contract.md). Preserve the authoritative failure signature and repair history. The controller owns retry budgets, G6, routing and durable case updates; the model supplies evidence, not a new counter or replacement signature. Two no-progress rounds, three failed fixes and five-round limits must be checked against durable history, not recalled conversation.

If root cause or verification is absent, use `DIAGNOSIS_INCOMPLETE` with explicit missing evidence. If a required controller/schema field cannot be truthfully represented, return a contract blocker; never manufacture a repair diff, identity or remote receipt to make validation pass.

## Remote output

Report target/role, time window and clock skew, request IDs and exit status, redacted observations, hypothesis/counter-evidence, next useful observation and cleanup status. Use only the resources loaded for this case. The full relay/session/process tooling is bundled here; no external debugging skill is required.
