---
name: tom-grill
description: Use when an iCafe requirement is ambiguous, carries no acceptance criteria yet, spans multiple repositories, contains unresolved tradeoffs, or needs a verified decision map before Spec.
---

# Tom Grill

## Overview

Close only the human decisions that block a reliable Spec, and record the acceptance criteria the requirement owner commits to. Facts belong to iCafe, code, project docs, GitNexus, or approved knowledge sources; do not ask the user for facts that can be verified. Acceptance criteria are not facts: they are intent that only the requirement owner can state, so asking for them is in scope.

## Inputs and Outputs

Consume an immutable `RequirementSnapshot`, repository/project evidence, and an optional `DecisionMap`. Produce exactly one result:

- `CLARIFIED`: every blocking decision has a Decision Log entry with question, options, decision, decision maker, evidence references, and input revision.
- `NO_OPEN_DECISIONS`: facts are sufficient and no artificial question was asked.

Also produce a glossary delta and an ADR candidate only when a decision is hard to reverse, surprising to future readers, and has a real alternative.

A run may start from a plain card whose `acceptance` list is empty. Record every criterion agreed with the requirement owner in `acceptance_delta` as `{id, statement, decided_by, evidence}`. Never rewrite the snapshot; the parent unions it with the delta before Spec. Either result may carry an `acceptance_delta`, and collecting acceptance criteria is not itself a decision.

Follow the [producer contract](../tom-autodev/references/producer-contract.md) and [decision-log schema](../tom-autodev/schemas/decision-log.schema.json). Return `decision-log` DraftContent; the parent creates the envelope, receipts and applicable gate. See [phase-specific rules](references/phase-contract.md).

## Comate structured decision protocol

When a human choice is required, make the choice explicit in Comate before submitting
the DraftContent. Do not send a long paragraph that mixes background, alternatives,
and the requested action. Use the structured question interface (`AskUserQuestion` or
the equivalent Comate question tool) so the owner can see one clear题目 and its选项.

Each question must have this shape:

```json
{
  "id": "repair_strategy",
  "title": "修复策略",
  "prompt": "本次需求采用哪种修复策略？",
  "context": "已核实的事实和为什么现在需要用户决定。",
  "options": [
    {"id": "A", "label": "恢复低位零守卫"},
    {"id": "B", "label": "增加兼容字段校验"},
    {"id": "C", "label": "其他"}
  ],
  "allow_multiple": false,
  "required": true,
  "other_allowed": true,
  "evidence": ["ku://固定分析文档", "repo://固定代码版本"],
  "impact": "选择结果会进入 Spec、验收点和后续实现范围。",
  "depends_on": []
}
```

Rules:

1. Ask only choices that facts cannot settle. Put the checked facts in `context` and
   `evidence`; do not make the owner re-confirm a repository path, revision, or log fact.
2. Use single-select for mutually exclusive alternatives and multi-select only when
   combining options is genuinely valid. The final option should be `其他` when an
   unlisted answer is plausible; selecting it requires supplementary text.
3. Include the consequence in `impact`, especially when the choice changes scope,
   acceptance, rollback, gray release, or a second repository.
4. Ask independent questions in one round. If question B depends on the answer to A,
   ask A first, then construct B from the selected answer. Never show options that are
   invalid under the current answer.
5. Do not fill an unanswered question with a recommendation. Keep it in
   `choice_questions` and `unresolved_frontier`.
6. After answers arrive, map them to the existing decision-log fields:
   `decisions[].choice`, `decisions[].rationale`, and `decisions[].evidence`.
   Confirmed acceptance criteria go to `acceptance_delta`; the immutable iCafe
   snapshot is never edited.
7. Submit a final `DECISIONS_RECORDED / COMPLETE` draft only when every required
   question has an answer. A partial round remains `UNRESOLVED / INCOMPLETE`.
   The Comate answer is not G1 approval: only after the answered draft is submitted
   may the worker create the hash-bound G1 approval.

For BGW-style scope decisions, prefer this compact presentation:

```text
题目 1：修复策略
说明：证据已定位到 syncookie 低位序号校验和零守卫缺失。
选项：
A. 恢复低位零守卫
B. 增加保留位、rcv_wscale、MSS 索引等值校验
C. 两种方案都实现
D. 其他（请补充）
影响：决定实现范围和验收标准。
```

For the current syncookie-style case, the first Comate round should normally cover
`修复策略` and `修改范围`; the next round can cover `回归验收` and `旁路措施`;
the final round covers `灰度 / 发布边界`. Use concrete choices such as:

- `修复策略`：A 根治方案；B 次选兼容校验；C 两种方案都实现；D 其他。
- `修改范围`：A 同时覆盖两个校验函数；B 只覆盖 nortt；C 只覆盖 rtt；D 其他。
- `回归验收`：A 覆盖乱序与 256 对齐偏移；B 只测正常握手；C 以
  `sp_bad_ack` / `sp_pkt_buf_overflow` / `tcp_out.rst` 指标为主；D A + C；E 其他。
- `旁路措施`：A 仅作应急旁路；B 允许网络/MSS 调整完成需求；C 同时纳入网络和
  BGW；D 其他。
- `灰度 / 发布边界`：A CRITICAL 快路径修改必须灰度后发布；B iPipe 回归通过即可；
  C 其他。

Do not copy these options blindly when the pinned evidence or project changes; they
are a presentation example, not facts or a default answer.

## Procedure

1. Read the requirement, acceptance points, attachments, current code, project rules, and knowledge references.
2. Record a Decision Map only when scope, repository boundaries, test interface, environment ability, or release boundary is unclear.
3. Verify facts independently. Add each unresolved human tradeoff to a queue ordered by dependency.
4. Convert the queue into structured Comate questions. Group independent choices into one round, split dependent choices into rounds, and include checked evidence plus the consequence of each option. Do not ask again for a decision explicit in the request or approved input.
5. If acceptance criteria are missing, propose observable criteria grounded in the request and ask the owner to confirm or amend the group. Existing owner-supplied criteria are already intent, not a reason to repeat confirmation. Each criterion needs a test or named observation point; source-reading details belong in Spec evidence. When the parent supplies `acceptance-candidates` from a pinned KU directory, treat them as unconfirmed drafts and carry each accepted candidate's source into its evidence. Never silently promote inferred criteria.
6. If no decision is required, return `NO_OPEN_DECISIONS`; do not invent a question to make the phase look complete.
7. After each answer round, return the updated Decision Log, the remaining `choice_questions`, and changed assumptions; the controller invalidates affected downstream context. Do not mutate the original snapshot or state ledger.

## Stop Conditions

Stop with `DECISION_REQUIRED` when a tradeoff lacks a decision. Stop with `ACCEPTANCE_REQUIRED` when snapshot `acceptance` and `acceptance_delta` are both empty. Stop with `REQUIREMENT_CHANGED` when the iCafe content hash changes. Do not create tasks, code, tests, or a release plan in this phase.

**REQUIRED PARENT:** Return the result and evidence references to `tom-autodev`; do not call iCode or iPipe directly.
