---
name: tom-diagnose
description: Use when Review, iPipe, release or product-test evidence needs root-cause diagnosis, or when an explicitly identified internal host needs bounded relay/tmux remote investigation of logs, processes, routing, VIP or BGW (x86bgw, bgwagent, newttm, controller) issues; when an InfoFlow group ID or link is supplied for chat-correlated diagnosis; or when a Higress/irouter/MCP/InfoFlow-bot chain fails (401/405/503/timeout/bot silent). 当用户给出内网机器名或 IP、要求登录机器看日志、定位网络/VIP/BGW 问题，给出如流群号或群链接要求结合群聊定位，或如流机器人、irouter 域名、Higress 网关、MCP 服务链路出现 401/405/503/超时/机器人不回复时使用。不用于纯本地操作、发版或未授权访问。
---

# Tom Diagnose

When the action carries `knowledge_workspace`, first read every listed file with its `show` command: this team-maintained project knowledge takes precedence over same-named content in the project skill, while business source code at the pinned revision remains the final source of truth.

## Select the context

| Supplied context | Mode and output |
|---|---|
| A run/action or frozen Review/iPipe/release failure bundle | **Workflow diagnosis:** return `diagnosis` DraftContent to the parent under the [producer contract](../tom-autodev/references/producer-contract.md). |
| An explicit host and a request to inspect it, without a workflow job | **Remote investigation:** use the bundled [remote operations](references/remote-operations.md); return a human-readable evidence report. No iCafe run, project registration or fake pipeline IDs are needed. |
| Workflow failure plus missing live-host evidence | Keep workflow mode. Present a bounded evidence request to the parent; use remote operations only for its authorized target/commands. Observations supplement, but never replace, iPipe execution receipts. |

Remote operation does not authorize deployment, an iPipe rerun or code repair. BNS/Matrix/container transports are unsupported by the bundled relay helper; require an exact supported SSH target instead of guessing.

## Select the domain

| Symptom area | Read |
|---|---|
| BGW data plane / control plane / newttm (x86bgw, bgwagent, controller/ACC, ECV, Offer Hash, TTM); BGW node/port history metrics via `scripts/sugar_query.sh`; BGW/NLB asset, VS config/RS and change history via `scripts/cmdb_query.py` (BSP CMDB) | [domains/bgw.md](references/domains/bgw.md) |
| Gateway / MCP / bot chain (InfoFlow bot, bridge, irouter/BFE, Higress route and key-auth, MCP service, model gateway) | [domains/higress-mcp.md](references/domains/higress-mcp.md) |
| User supplies an InfoFlow group ID or link | [infoflow-evidence.md](references/infoflow-evidence.md) |

Domain files are methods and signal semantics. Concrete environment facts (hosts, domains, ports, topology) come from the workspace/environment docs, not from these files.

## Diagnose from evidence

Read [root-cause protocol](references/root-cause-protocol.md). Freeze the failure identity, business/test revisions, stage/job, environment and log window; compare with a valid passing baseline. Trace the first broken component boundary, form one falsifiable hypothesis, and specify a minimal discriminating observation. Keep observed facts, inference and unknowns separate.

Read project runtime topology only for the selected project; NPL compile evidence also uses the suite's `tom-lang-npl` diagnostics. Only confirmed Review findings are failure inputs; a question about intended behavior returns to the requirement owner.

Environment, transport, revision and stale-artifact failures do not justify business patches. A flaky or non-reproduced failure needs additional evidence, not repeated speculative edits. Do not edit code in workflow diagnosis; repair implementation belongs to the approved PLAN/IMPLEMENT path.

## Workflow output and limits

Use the action's [diagnosis schema](../tom-autodev/schemas/diagnosis.schema.json) and [phase-specific mapping](references/phase-contract.md). Write the human-readable fields that G6 shows to approvers (`hypothesis`, `repair_direction`, `comparison`, `log_evidence`, `reproduction`, `minimal_verification`, `repair_plan`) in Chinese; keep enums, identifiers, file paths and the authoritative failure signature verbatim. The approval card shows any untranslated English as labeled source text instead of guessing a translation. Preserve the authoritative failure signature and repair history. The controller owns retry budgets, G6, routing and durable case updates; the model supplies evidence, not a new counter or replacement signature. Two no-progress rounds, three failed fixes and five-round limits must be checked against durable history, not recalled conversation.

If root cause or verification is absent, use `DIAGNOSIS_INCOMPLETE` with explicit missing evidence. If a required controller/schema field cannot be truthfully represented, return a contract blocker; never manufacture a repair diff, identity or remote receipt to make validation pass.

## Reusable workspace knowledge

The optional `knowledge_candidates` array contains only evidence-backed environment/process conclusions that are reusable across runs. Do not include business-code conclusions, run-specific fixes or unverified hypotheses; omit the field or use `[]` when nothing qualifies. Each candidate has non-empty `target`, `title`, `body` and `reason` strings. Use `reason` to cite the supporting evidence and explain cross-run reuse; write `title`, `body` and `reason` in Chinese while preserving source identifiers.

A target must match `^docs/[a-z0-9-]+(/[a-z0-9._-]+)*\.md$`, contain no `..`, and have a basename other than `index.md`, `template.md` or `README.md`. Provide only the reusable body, not an index/change-log edit. Candidates are proposals, not workspace writes: after `RELEASE_SUCCESS`, the controller may aggregate approved G6 diagnoses and request a separate `KNOWLEDGE_WRITEBACK` approval before opening a workspace CR for human review. The producer must not edit workspace files or push a CR during DIAGNOSE.

## Remote output

Report target/role, time window and clock skew, request IDs and exit status, redacted observations, hypothesis/counter-evidence, next useful observation and cleanup status. Use only the resources loaded for this case. The full relay/session/process tooling is bundled here; no external debugging skill is required.
