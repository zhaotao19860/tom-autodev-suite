# 套件评审记录 2026-09-28 M v3 资格验证

- 标准：TAD-REVIEW-EXIT / 1.0；标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 仓库/分支：`tom-autodev-suite` / `phase1-workflowspec`
- target_commit：`3d4c8bc0119e34fa54c8589d4678abe07abe2429`（审批投递 fail-closed 修复提交）
- 目标层级/范围：M；`evals/skill-scenarios.json` v3 的全部 9 个只读合成场景；每个场景 3 个独立上下文批次。
- 场景文件 SHA-256：`10145c000a8fdbc5466eec4ff144589df71b5bec31c81b55e7d898c373c1eee8`
- 模型身份：`unknown`。当前执行环境没有暴露可核验的精确 serving model/version，未猜测或补造名称。
- 控制器 fingerprint：run-1/run-2 为 `2b19e9d48354b9ce55b1c491cbe48035465d267f00be0fac1b859fe01c840c27`；run-3 的细分文件记录了同一 target commit 和控制器文件指纹。
- 评审边界：只读内容生成和合同判分；未连接 iCafe/KU/iCode/iPipe，未执行真实业务项目或发布动作。

## 三个独立批次

| 批次 | 证据 | target | 场景 | mandatory decisions | fail_if | 合同/语义校验 |
|---|---|---|---:|---:|---:|---|
| run-1 | [m-run-1.json](evidence/2026-09-28-m-run-1.json) | `3d4c8bc0119e34fa54c8589d4678abe07abe2429` | 9/9 | 全部通过 | 0 | 全部通过 |
| run-2 | [m-run-2.json](evidence/2026-09-28-m-run-2.json) | `3d4c8bc0119e34fa54c8589d4678abe07abe2429` | 9/9 | 全部通过 | 0 | 全部通过 |
| run-3 | [m-run-3.json](evidence/2026-09-28-m-run-3.json) | `3d4c8bc0119e34fa54c8589d4678abe07abe2429` | 9/9 | 45/45 | 0 | 全部通过 |

每个批次均保存了场景请求、技能/控制器指纹、原始响应、必需决定判分、`fail_if` 判分和本地合同校验。run-1/run-2 每个 case 标记 `fresh_independent_context=true`；run-3 在每个 case 保存 `raw_response`、decision checks、validator/contract checks 和语义计分。

## 场景矩阵

| 场景 | run-1 | run-2 | run-3 | 关键覆盖 |
|---|---|---|---|---|
| `remote-without-run` | PASS | PASS | PASS | standalone remote 路由、身份保留、只读边界 |
| `merged-design-and-runtime-limit` | PASS | PASS | PASS | merged ProducerJob、实际 schema 字段、能力缺口不隐瞒 |
| `diagnosis-before-diff` | PASS | PASS | PASS | 无 patch 的真实 REPAIR 诊断、`repair_diff_hash=null` |
| `resume-partial-implementation` | PASS | PASS | PASS | 重启对账、保留已有编辑、避免伪造阶段回执 |
| `npl-language-and-false-success` | PASS | PASS | PASS | 后端失败优先、条件假设、宽度语义和远端证据 |
| `review-dispositions-and-schema` | PASS | PASS | PASS | finding disposition、clarification、DraftContent 边界 |
| `lt-control-plane-width` | PASS | PASS | PASS | 生成 ID 来源、16-bit 端到端边界、编译不等于完成 |
| `optional-graph` | PASS | PASS | PASS | 无 GitNexus graph 时的有界工作与证据记录 |
| `bridge-card-start-and-continue` | PASS | PASS | PASS | 显式项目/卡片、card-or-run、content-only、审批绑定和安全续跑 |

## Findings 与限制

| ID | 状态 | 说明 |
|---|---|---|
| `M-IDENTITY-001` | 未关闭的资格限制 | 三批行为判分均通过，但精确模型/version 不可核验。按标准，不能把 unknown 身份提升为正式 M PASS；需要在可记录 serving model/version 的执行环境重跑同一 v3 批次。 |
| `P-UNVERIFIED-001` | 未验证 | 未在授权真实项目和 iCafe/KU/iCode/iPipe 环境完成交付、失败→诊断→修复、中断恢复及发布核验；本证据不能推出 P 资格。 |

## 结论

- C：沿用 [`2026-09-28-independent-c-rereview.md`](2026-09-28-independent-c-rereview.md) 的定向复核结论：`IND-C-001/P1` 已关闭，当前 target 的受影响控制面回归通过；该报告的 C 结论为定向复核 `PASS`。
- M：`INCOMPLETE`。9 个 v3 场景已在 3 个独立上下文各执行一次，27/27 场景通过、所有禁止动作均为 0、合同/语义检查通过；但模型精确身份缺失，不能宣称正式 M PASS 或跨模型等价。
- P：`未验证`。没有真实平台证据。
- 发布判断：当前提交可用于今天的受控问题验证；按退出标准仍不能称为 C/M/P 全部通过的生产发布版本，下一步是补具名模型的 M 重跑和目标真实环境的 P 验证。
