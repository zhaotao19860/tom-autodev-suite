# 套件评审记录 2026-09-28-independent-c

- 标准：TAD-REVIEW-EXIT / 1.0；标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 仓库/分支：`tom-autodev-suite` / `phase1-workflowspec`
- base_commit（近期直接影响范围）：`4d58c0446d682b2e15a9835079dcdd03374a599d`；target_commit：`2fcb4aa785bc826da0796338e671515aead4db74`
- 近期范围：`git diff 4d58c04..HEAD`，42 个文件，3957 行新增、367 行删除；另对 target commit 的完整代码、skill、脚本、schema、文档和调用边界做了复审。
- 目标层级/范围/支持环境：控制面工程验收 C；WF-01..WF-10，近期 Worker/CLI/审批投递/iPipe 监控/进度卡片/发布身份改动；本地 Python 3.9.6、Node.js v25.9.0。未执行真实 iCafe/KU/iCode/iPipe 发布。
- workflow/profile/schema/skill/prompt 身份：`workflow_spec.py` SHA-256 `f7d4157b11172de04c489b0128e20eccbb7c316236527e85342a595d5abb4cd1`；schema 集合 SHA-256 `d36a973e5233021c45afcee15e16acd73aa80ffd628086a485e0ad255ee248b0`；`tom-autodev/SKILL.md` `e0ab7a204f795e72ac057e7ed3ee80fe2538b48be08293e4413a70c62a8db91f`；`tom-review/SKILL.md` `9267f5f16b8495d963d82d60d04f41d37cdee3ecdb49d1808370267d9c50c0a9`；`tom-diagnose/SKILL.md` `81a82529591c1558777e7d61b61be7e5a2bdd5dd9af2982b723cd332daf80847`。
- 评审者/模型版本/日期：独立 C 复审代理；模型版本未知；2026-09-28（Asia/Shanghai）。
- 负责人：由 `/root` 指定；预算与已用轮次：一轮独立初审。
- 继承证据：不继承历史结论；本报告只绑定上述 target commit。本报告的复现证据见 [`2026-09-28-independent-c-approval-delivery.txt`](evidence/2026-09-28-independent-c-approval-delivery.txt)。

## 验收矩阵

| WF ID | 证据、用例/命令、实际结果 | 状态 |
|---|---|---|
| WF-01 | 完整控制面测试 `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q`：1043 tests，`OK`；对应 producer/schema 入口为 `test_producer_validation.py`、`test_schema_validation.py`。静态核对 `worker_driver` 只接收 DraftContent、`phase_protocol.next(read_only=True)` 不写 action 缓存。 | 有证据；被 IND-C-001 阻断的审批边界仍需修复后复核 |
| WF-02 | 完整控制面测试通过；审批重试/通道用例覆盖 `test_approval_gate_retry.py`、`test_approval_channels.py`。独立生产形态复现发现 CLI 审批投递失败被错误转为成功 PARKED（IND-C-001）。 | 失败（P1） |
| WF-03 | 完整控制面测试通过；`test_workspace_recovery.py`、`test_task5_safety.py`、`test_worker_concurrency_contract.py` 覆盖工作区/任务绑定。静态核对 `AgentBridge` 通过 `resolve_run_target` 只取持久化 INTAKE 身份。 | 有证据 |
| WF-04 | 完整控制面测试通过；`test_phase_protocol.py`、`test_icode_runtime.py`、`test_state_and_artifacts.py` 覆盖检查点/未知结果。静态核对外部 intent 先 claim，未知回执保留 pending，不能盲重发。 | 有证据 |
| WF-05 | 完整控制面测试通过；`test_orchestrator.py`、`test_worker_concurrency_contract.py` 覆盖 run lease。静态核对 CLI `process/drive/continue/submit-draft` 注入 `orchestrator.recovery.locks`，`submit_draft` 和 `advance` 使用同一 run lease；维护入口仍明确是独立边界。 | 有证据 |
| WF-06 | 完整控制面测试通过；`test_pipeline_plan.py`、`test_ipipe_runtime.py` 覆盖冻结计划、版本和监控。静态核对 `frozen_plan`、`release_builds`、`build_matches` 绑定 revision/environment。 | 有证据 |
| WF-07 | 完整控制面测试通过；`test_release_evidence_pinning.py`、`test_pipeline_plan.py` 覆盖多模块发布证据。静态核对 RELEASE 只读 watcher 与 worker 均按 frozen plan、build binding 和 release proof 校验。 | 有证据 |
| WF-08 | 完整控制面测试通过；`test_execution_guards.py`、`test_workflow_spec.py`、`test_profile_repin.py` 覆盖 drift/repin。静态核对 `execution_guard` 位于 CLI、watcher、runtime 写入口前。 | 有证据 |
| WF-09 | 完整控制面测试通过；`test_phase_protocol_repair.py`、`test_repair_policy.py`、`test_failure_signatures.py` 覆盖持久化预算与根因签名。静态核对诊断修复预算从 run 事件和 failure case 计算，不信任模型自报计数。 | 有证据 |
| WF-10 | 完整控制面测试、tom-review、tom-diagnose 和 diff check 均通过；`test_runtime_contracts.py`、`test_run_summary.py` 覆盖回执/摘要。未进行真实平台发布，因此真实平台证据仍未验证。IND-C-001 证明 CLI 顶层结果可伪称等待审批成功，属于完成回执真实性缺口。 | 失败（P1 影响）；真实平台资格未验证 |

### 固定命令结果

- `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q`：`Ran 1043 tests in 141.200s`，`OK`。
- `python3 -m unittest discover -s tom-review/tests -p 'test_*.py' -v`：6 tests，`OK`。
- `bash tom-diagnose/tests/test_autodebug.sh`：`PASS: tom-autodebug regression tests`。
- `git diff --check`：退出码 0，无输出。

## Findings

| ID / 严重级别 | 违反合同 | 触发、预期/实际、影响 | 当前证据及反证核对 | 处置与修复/复核状态 |
|---|---|---|---|---|
| **IND-C-001 / P1** | WF-02：审批只授权准确动作、输入和版本；投递失败不能取得完成资格。WF-10：完成结论和回执必须真实。 | 触发：`submit-draft`/`drive`/`continue` 返回 `APPROVAL_REQUIRED`，随后 `_request_approval` 的真实结果为审批账本字段 `status=DELIVERY_FAILED`、`reason_code=APPROVAL_DELIVERY_FAILED`、`retry_allowed=true`，但没有顶层 `ok`；典型条件是一渠道客户端在发送前抛 `AttributeError`、另一渠道已成功。预期：CLI 返回失败并保留可重试状态。实际：`_deliver_worker_approval` 仅在 `approval.get("ok") is False` 时失败（`tom-autodev/scripts/orchestrator.py:3561-3587`），于是返回 `ok=true, reason_code=PARKED, parked=APPROVAL_WAIT`，CLI 退出 0。影响：用户会以为审批卡已成功投递，若不再次主动调用 `continue`，run 会长期停在未获批闸门；正常门控流程被错误完成提示遮蔽。 | 代码调用链：`request_infoflow_approval`（`orchestrator.py:1747-1779`）将账本字段与失败字段合并但不保证 `ok=false`；`_deliver_worker_approval`（`orchestrator.py:3572-3587`）据 `ok` 缺省值进入 PARKED。生产形态复现记录于 [`evidence/2026-09-28-independent-c-approval-delivery.txt`](evidence/2026-09-28-independent-c-approval-delivery.txt)：输出同时含 `ok=True`、`reason_code=PARKED` 和 `approval.status=DELIVERY_FAILED`。已有 `test_cli_operations.py` 只用人为构造的 `{"ok": false, ...}` 反例，未覆盖真实账本返回形态，因此不能排除。 | **确认缺陷，阻断 C。** 修复应让 `_deliver_worker_approval` 识别 `status=DELIVERY_FAILED`、`reason_code`/`delivery_failure` 等真实失败字段，保持 `ok=false`、失败 reason 和 retry 信息；增加 production-shaped 回归后，复核 CLI exit code、审批账本、重试与未知结果路径。 |

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/理由 | 负责人及接受决定证据 | 到期/重开条件 |
|---|---|---|---|---|
| LIMIT-C-001 | M：未按 8 个固定场景、每场景 3 次执行确切模型/skill/prompt 身份；未保存模型原始响应。 | 本轮目标是 C；不能据此推断模型资格。 | 待负责人决定；无风险接受记录。 | 声称 M 通过前必须按 `evals/skill-scenarios.json` 执行并归档原始响应。 |
| LIMIT-C-002 | P：未在授权真实 iCafe/KU/iCode/iPipe 环境完成正常交付、失败→诊断→修复、中断恢复和多模块发布。 | 本地测试不能冒充业务平台证据。 | 待负责人决定；无风险接受记录。 | 声称 P 通过前需归档真实 run/CR/revision/build/环境/审批/发布核验。 |
| P3-001 | `preflight.local_automation_status` 用 `orchestrator.py` 是否存在表示 approval/ipipe watcher command available（`preflight.py:130-146`），而用户文档示例入口为 `cli.py watch-approvals/watch-ipipe`。这是诊断可读性改进，不改变控制面结果。 | 当前报告不将其列为缺陷；修复时统一探针路径和文案。 | 负责人待排期。 | 若预检被用于自动化就绪 gate，需重开并增加入口存在性回归。 |

## 结论

- C：**BLOCKED**。依据：target commit 的完整本地命令均通过，但确认的 `IND-C-001 / P1` 使审批失败可被报告为成功 PARKED，违反 WF-02/WF-10；修复并取得 production-shaped 回归前不能退出。
- M：**未验证**。本轮未运行模型资格场景，不声称跨模型/skill 通过。
- P：**未验证**。本轮未调用真实业务平台，不声称真实发布资格。
- 局部复核结论：近期 42 文件/3957 行直接影响范围已独立检查；不能提升为 M/P 或生产就绪。
- 未验证项与下一步：先修复并回归 IND-C-001（包括真实账本无 `ok` 形态和 CLI exit code），再按第 5 节预算做一次定向复核；M/P 另行取证。由于存在 P1，停止开放式评审；新 P0/P1、真实运行违约或修复回归失败时重开。

> 工作区在本报告写入时另有其他代理产生的未提交测试/证据文件；这些文件不属于 target_commit，本报告的命令结果取自 target commit 代码基线。
