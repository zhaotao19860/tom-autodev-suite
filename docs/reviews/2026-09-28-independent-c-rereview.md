# 套件评审记录 2026-09-28-independent-c-rereview

- 标准：TAD-REVIEW-EXIT / 1.0；标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 仓库/分支：`tom-autodev-suite` / `phase1-workflowspec`
- base_commit（修复前固定 target）：`2fcb4aa785bc826da0796338e671515aead4db74`；target_commit（修复后完整提交）：`3d4c8bc0119e34fa54c8589d4678abe07abe2429`
- 修复差分范围：2 个文件，`git diff 2fcb4aa..3d4c8bc` 为 46 行新增、2 行删除；生产代码仅修改 `tom-autodev/scripts/orchestrator.py`，另增 CLI 回归用例。
- 目标层级/范围/支持环境：对初审 finding **IND-C-001 / P1** 的定向复核；范围为 `_deliver_worker_approval` 的审批投递结果判定、CLI 顶层退出结果、审批正常成功/失败路径及相关回归。环境为本地 Python 3.9.6；未执行真实 iCafe/KU/iCode/iPipe 发布。
- workflow/profile/schema/skill/prompt 身份：沿用初审固定身份；本修复未改 workflow/profile/schema/skill。初审报告：[`2026-09-28-independent-c-review.md`](2026-09-28-independent-c-review.md)。
- 评审者/模型版本/日期：独立 C 复审代理；模型版本未知；2026-09-28（Asia/Shanghai）。
- 负责人：由 `/root` 指定；预算与已用轮次：一次修复→定向复核。
- 继承证据：初审 target `2fcb4aa785bc826da0796338e671515aead4db74` 的完整控制面测试 1043 tests、tom-review 6 tests、tom-diagnose 回归及 diff check 均通过；本报告只验证修复差分及其影响面，并将结论绑定到本报告 target commit。

## 验收矩阵

| WF ID / 场景 | 证据、用例/命令、实际结果 | 状态 |
|---|---|---|
| WF-02 审批投递失败不得取得 PARKED 成功资格 | `test_cli_operations.py` 31 tests 通过；新增 `test_approval_delivery_failure_without_ok_field_changes_top_level_result` 用生产账本形状 `status=DELIVERY_FAILED`、`reason_code=APPROVAL_DELIVERY_FAILED`、无顶层 `ok`，断言结果 `ok=false`、失败 reason 保留。另以同形状调用 `main submit-draft`，实测 JSON 为 `ok=false, reason_code=APPROVAL_DELIVERY_FAILED, retry_allowed=true`，CLI `exit_code=1`。 | 通过 |
| WF-02 合同形状的正常审批请求仍可停车等待 | 新增 `test_contract_shaped_pending_approval_is_accepted_without_ok_field`，`status=PENDING` 且非空 `approval_id` 仍返回 `ok=true, reason_code=PARKED, parked=APPROVAL_WAIT`；CLI 正常成功路径 `test_submit_draft_loads_content_json_and_delegates_only_content`、`test_process_new_card_starts_then_drives_and_delivers_approval` 同批通过。 | 通过 |
| WF-02 审批通道/重试边界未回归 | `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_approval_channels.py' -q`：10 tests，OK；`test_approval_gate_retry.py`：20 tests，OK；`test_approval_delivery.py`：32 tests，OK。 | 通过 |
| WF-04/WF-05 调用方和状态迁移未回归 | `test_orchestrator.py`：53 tests，OK；CLI 31 tests，OK。修复只改变审批投递结果的失败闭合，不改变 lease、状态恢复或审批账本写入。 | 通过 |
| 代码完整性 | `git diff --check`：退出码 0，无输出。target commit 为已提交完整 SHA；工作区未跟踪的历史审查证据不属于 target。 | 通过 |
| WF-01、WF-03、WF-06、WF-07、WF-08、WF-09、WF-10 | 沿用初审在 `2fcb4aa...` 的完整控制面证据；修复差分只触及审批投递封装和对应回归测试，未扩大这些不变量的代码范围。 | 继承有效证据 |

## Findings

| ID / 严重级别 | 违反合同 | 触发、预期/实际、影响 | 当前证据及反证核对 | 处置与修复/复核状态 |
|---|---|---|---|---|
| **IND-C-001 / P1** | WF-02：审批只授权准确动作、输入和版本；投递失败不能取得完成资格。WF-10：完成结论和回执必须真实。 | 初审触发为 `_request_approval` 返回真实审批账本形状：`status=DELIVERY_FAILED`、`reason_code=APPROVAL_DELIVERY_FAILED`、无顶层 `ok`。旧实现把它当成功 `PARKED/APPROVAL_WAIT`。修复后只有显式 `ok is True`，或合同形状的 `status=PENDING` 且非空 `approval_id` 才算成功；其他结果统一返回 `ok=false`，保留失败字段、`worker_result` 和可重试信息。 | 静态核对 `orchestrator.py:_deliver_worker_approval`；新增生产形状回归在 31 个 CLI 用例中通过；独立 `main submit-draft` 复现输出 `exit_code=1`；`approval_delivery`、`approval_channels`、`approval_gate_retry`、`orchestrator` 相关套件均通过。正常 `ok=true` 成功路径和 `PENDING+approval_id` 路径均有通过证据。 | **已修复并关闭。** P1 触发不再把部分投递失败报告为成功；正常审批停车及重试相关路径未回归。 |

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/理由 | 负责人及接受决定证据 | 到期/重开条件 |
|---|---|---|---|---|
| LIMIT-C-001 | M：未按 8 个固定场景、每场景 3 次执行确切模型/skill/prompt 身份；未保存模型原始响应。 | 本轮仍是 C 及 IND-C-001 定向复核，不能据此推断模型资格。 | 待负责人决定；无风险接受记录。 | 声称 M 通过前按 `evals/skill-scenarios.json` 执行并归档原始响应。 |
| LIMIT-C-002 | P：未在授权真实 iCafe/KU/iCode/iPipe 环境完成正常交付、失败→诊断→修复、中断恢复和多模块发布。 | 本地测试不能冒充业务平台证据。 | 待负责人决定；无风险接受记录。 | 声称 P 通过前需归档真实 run/CR/revision/build/环境/审批/发布核验。 |
| P3-001 | 初审记录的 `preflight.local_automation_status` 入口文案/探针可读性建议未在本修复中处理。 | 不影响本 finding 或控制面结果，保持改进项。 | 负责人待排期。 | 若预检被用于自动化就绪 gate，需重开并补入口存在性回归。 |

## 结论

- C：**PASS（针对初审阻断项的定向复核）**。依据：`3d4c8bc0119e34fa54c8589d4678abe07abe2429` 已提交；IND-C-001/P1 的生产形状回归通过，正常成功与审批重试相关路径通过；初审固定 target 的完整 C 证据可沿修复差分追溯到最终提交，且没有新增 P0/P1。
- M：**未验证**。本轮未执行模型资格场景，不声称跨模型/skill 通过。
- P：**未验证**。本轮未调用真实业务平台，不声称真实发布资格。
- 局部复核结论：**PASS**；IND-C-001 已关闭，正常成功路径未破坏。该结论不提升为 M/P 或真实生产平台资格。
- 未验证项与下一步：M/P 仍需独立取证；若审批接口引入新的成功响应形状，应补充明确契约回归。若出现新的 P0/P1、真实运行违约或本修复回归失败，按标准重开。
