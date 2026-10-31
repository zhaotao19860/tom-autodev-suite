# 套件评审记录 R9（局部 C：MVP 第 2 步收尾）

- 标准：TAD-REVIEW-EXIT / 1.0；标准 SHA-256：5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393
- 仓库/分支：tom-autodev-suite / mvp
- 范围：aca2b33 → c6dedf8（26 个文件：预授权、scope 授权接入 worker、合同 3.4 RESTART、ENV_WAIT、iPipe 证据与恢复修复）
- 方式：第 5 节第 1 条两份独立初审（GPT-6.1-Sol via comatecli、codex），之后两轮定向复核（预算 2/2 用完）

| 轮次 | 基线 → 目标 | GPT | codex |
|---|---|---|---|
| 初审 | aca2b33 → c6dedf8 | BLOCKED：3 P1 + 2 P2 | BLOCKED：7 P1 + 3 P2 |
| 复核 1 | c6dedf8 → 3ff3ee1 | BLOCKED：2 P1 + 1 P2（含修复引入 R9-NEW-001） | BLOCKED：2 P1 + 1 P2（C-05 终态别名） |
| 复核 2 | 3ff3ee1 → a4733d1 | BLOCKED：R9-NEW-001 未关闭（3ff3ee1 期间写入的 v4 哈希） | 未发现 P0/P1/P2，四项全部关闭 |

## finding 终态

| ID | 级别 | 终态 | 关闭 commit |
|---|---|---|---|
| C-01/02/03（scope 门集合、成员交集、重发卡） | P1 | 已关闭 | 3ff3ee1 |
| C-04/RSTR-002（RESTART 检查与提交之间的并发） | P1 | 已关闭 | 3ff3ee1 |
| C-05/RSTR-001（排除 build、终态白名单） | P1 | 已关闭 | a4733d1 |
| C-06/RSTR-004（修复预算继承） | P1/P2 | 已关闭 | 3ff3ee1 |
| C-07（基线 build 身份） | P1 | 已关闭 | 3ff3ee1 |
| C-08（ENV_WAIT 到期补 STOP） | P2 | 已关闭 | 3ff3ee1 |
| C-09/ENV-001（ENV_WAIT 只等真实缺失） | P2 | 已关闭 | a4733d1 |
| C-10/RSTR-003（需求差异与人工 GRILL） | P1 | 已关闭 | a4733d1 |
| R9-NEW-001（v4 哈希兼容，修复引入） | P1 | 预算外修复，未经第三轮复核 | 见下 |

## 预算外处理

复核 2 中 GPT 仍报 R9-NEW-001：3ff3ee1 存活约 75 分钟（01:03–02:18），期间若建了带需求差异的重开 run 或 scope run，其 v4 哈希含 `requirement_diff` 键。核对状态库：没有任何 `intake_hash_version` 为 v4/v5 的 INTAKE，实际不存在受影响的 run。仍按 GPT 建议补了兼容读取：v4 payload 先按 v4 字段校验，失败时再按 3ff3ee1 的字段集合校验，其他版本不受影响；新增回归 1 个，全量 1640 个通过。该修复在第 5 节预算之外，未取得复核，作为残余风险登记。

## 结论

局部 C：两份初审的全部 P1/P2 已修复；最终复核一份 PASS、一份 BLOCKED（R9-NEW-001，已在预算外修复且实际无受影响 run）。按第 5 节第 4 条，本批次停止评审。残余风险：R9-NEW-001 的兼容分支未经独立复核；scope 授权与 ENV_WAIT 尚无真实 run 证据（待合并验证 run）。C/M/P 全量资格未在本批次验证。
