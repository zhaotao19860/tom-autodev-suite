# 套件评审记录：2026-09-23 独立复审（GAP-WF08 定向取证）

**本轮是对首次 C 基线的第二份独立评审（标准 §5.1）。逐入口核对 GAP-WF08 所列可写入口后，未发现新的确认 P0/P1/P2；被标记"待取证"的入口都有充分约束（工作流漂移守卫 + 内容绑定审批 + 逐阶段 profile 重校 + 逐写 profile 重校），GAP-WF08 的 C 必要证据已具备。是否据此把 C 由 INCOMPLETE 收为 PASS，属负责人决定；M/P 仍未验证。** 本轮不改动任何控制面/ schema 代码。

## 固定基线与范围

- 标准：`TAD-REVIEW-EXIT / 1.0`；文件 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。本轮未修改标准或降低门槛。
- 仓库：`tom-autodev-suite`；分支：`phase1-workflowspec`。
- `target_commit`：**`d0e594113714720089a7f35601fd3c81cc052338`**。与首次评审的正式 target `daa76f6` 相比，`git diff --name-only daa76f6..d0e5941` 仅含 `docs/reviews/*` 与 `README.md`；`*.py` 与 `schemas/*` 零差异。故本轮所审代码与首次评审 target 逐字节一致，证据可直接绑定。
- 目标：C 工程验收的定向复审，范围限定为 [GAP-WF08 后续任务](2026-09-22-profile-guard-followup.md) 所列可写入口——知识发布/协作、审批与后台、维护恢复与摘要——是否在外部写/新授权前受配置漂移约束。未重审 WF-01…WF-10 其余未变逻辑。
- 已取证环境：macOS (darwin 25.6.0)、Python 3.9.6；临时 Git/SQLite、真实控制器与模拟外部平台。未验证 Linux/跨主机共享 FS、真实 Comate/KU/如流、真实业务构建或发布。
- 评审者：Claude（Opus 4.8）主上下文 + 一个独立只读探索子上下文交叉核对调用链；与首次评审的 Codex 上下文相互独立。
- 负责人：Tom。本轮不代负责人接受风险，也不代其宣布 C PASS。
- 预算：一轮定向复审，零修复轮（无确认缺陷需要修复）。

## 复核方法与全量校验

从仓库根目录复跑 §4 完整 C 命令（绑定本 target）：

| 命令 | 实际结果 |
|---|---|
| `python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q` | 976 tests，197.1s，OK |
| `python3 -m unittest discover -s tom-review/tests -p 'test_*.py' -v` | 6 tests，OK |
| `bash tom-diagnose/tests/test_autodebug.sh` | PASS，退出 0 |
| `git diff --check` | 退出 0，工作区干净 |

配置漂移守卫的两种机制（本轮据此判定各入口）：
- **workflow_spec 漂移**：`@guard_execution` / `execution_guard`（`execution_guard.py`）只调用 `workflow_spec.spec_drift`，**不读 profile**。
- **profile 漂移**：重读盘上 profile 文件、SHA-256 与运行钉住的 `profile_hash` 比对，不符返回 `PROFILE_CONFLICT`。分布在 `_runtime_profile`（`orchestrator.py:1662`）、`knowledge_sync()` 工厂（`orchestrator.py:788-798`）、`phase_protocol._pinned_profile_error`（`phase_protocol.py:2233`，经 `_next` 于 `:181` 在每次 `phase_protocol.next()` 触发）、以及 `icode_runtime._profile_error` / `ipipe_runtime._profile_error`（逐写重校）。

## GAP-WF08 逐入口证据

| 入口组 | 外部写点 | 支配写点的 profile 约束 | 反证核对结论 |
|---|---|---|---|
| 知识发布 `KnowledgeSync.publish_phase` | `ku.create_artifact/update_index/ensure_run_root`、`cafe.comment`（`knowledge_sync.py:176/190/296/204`） | 目标由 profile 派生，但：① 工厂 `knowledge_sync(run_id)` 构造时重哈希盘上 profile（`orchestrator.py:788-798`，`PROFILE_CONFLICT`）；② 每阶段经 `phase_protocol._next→_pinned_profile_error`（`:181`）在三处 `publish_phase`（`:704/1526/1753`）前重读盘上 profile；③ 默认流 `complete_phase` 每次重建对象（`orchestrator.py:587`）。既有回归 `test_orchestrator.py:257` 断言工厂 `PROFILE_CONFLICT`。 | 充分。默认流逐阶段 profile 受控，无长缓存漏洞 |
| `ku_client` 写方法 | `_republish/_publish_index/…` | 仅在 profile 受控的 `KnowledgeSync` 工厂内构造；写点取构造时绑定的 `doc_id/repo_id`，不重读 profile。另有一处只读 advisory 路径（`orchestrator.py:2104` `requirement_acceptance`，不写） | 充分。目标随 `KnowledgeSync` 每阶段重建而受控 |
| 协作 `CollaborationSession.create` / `send_message` | `group_client.create_or_reuse/send_markdown`（`collaboration.py:267/319`） | 无 profile 重哈希（仅 `@guard_execution`），但 `create` 要求内容绑定的 G0 审批（`input_hash` 绑定 group_name/成员快照），`send_message` 用持久化的 `group_id`；投递目标与内容在 INTAKE 固化、不随 live profile 重派生 | 充分。内容绑定审批 + 持久会话比 profile 重校更强；漂移无法改写投递对象或内容 |
| 审批与后台 `request_/reissue_/wait_infoflow_approval`、`ApprovalWatcher` 循环 | `_deliver_approval_channel`/`deliver_markdown`、`infoflow_client.wait`（`orchestrator.py:1721/1857`、`approval_delivery.py:156`、`approval_watch.py:202/442/551/828`） | 三个控制器方法均带 `@guard_execution`（workflow_spec 受控）；`member_policy` 与传输 client 皆为**调用方入参**，`ApprovalWatcher._reissue` 复用原审批行里已 profile 校验过的 `member_policy`（`approval_watch.py:492`）与 `input_hash`。**无 live profile 值流入这些投递** | 见下"P3 观察"。反证：漂移不改变投递对象/内容/授权范围（全部由原审批固化或调用方传入），且下游阶段动作再受 `phase_protocol._next` profile 重校 |
| 维护/摘要 `RunSummary.build/propose/apply`、`optimize`、`Recovery.resume` | `build` 仅本地 `artifacts.put`；`propose/apply` 经 `_archive→publish_phase`；`resume` 只读 | `optimize` 入口先 `_runtime_profile`（`orchestrator.py:616`）再经 `_owned_knowledge_sync`（`:626`，内含工厂 profile 校验 + 目标一致性二次校验 `:827`）；`RunSummary`/`knowledge_sync` 每次 `optimize()` 重建 | 充分。默认流 profile 受控；仅"绕过 optimize 直接注入陈旧 knowledge_sync"这一注入-API 情形不受保护，属注入纪律（P） |
| worker 驱动循环 `advance/_drive/resume` | 透传 `knowledge_sync=` 至 `complete_phase` | 默认 `knowledge_sync=None`（两处内部调用点 `worker_driver.py:1034/1145` 亦仅透传形参，生产无非-None 注入），每阶段经工厂重建并 profile 校验；即便注入对象被跨阶段复用，知识写仍受 `phase_protocol._next`（`:181`）逐阶段 profile 重校，icode/ipipe 写各自逐写重校 | 充分。默认流安全；注入对象复用是刻意保留的可信接缝（`complete_phase` 接受裸 `object()`，见 `test_orchestrator.py:1010`），其纪律归 P |

## Findings

| ID / 级别 | 违反合同 | 触发、预期/实际、影响 | 当前证据及反证核对 | 处置 |
|---|---|---|---|---|
| IR-01 / P3 | WF-08 一致性（非确认缺陷） | 触发：run 的盘上 profile 被非重钉方式改动。`ApprovalWatcher` 与 `reissue_/request_/wait_infoflow_approval` 只做 workflow_spec 漂移守卫，不重哈希 profile；CLI 包装 `_request_approval/_reissue_approval` 会经 `_runtime_profile` 重校（`orchestrator.py:3271`），watcher 的 `_reissue`（`approval_watch.py:488`）直连方法绕过它。实际影响：向**原审批已固化的同一批成员**重发**同一** `input_hash` 的门禁提醒/重发；不改写投递对象、内容或授权范围。 | 证据：`approval_watch.py:466-503`、`orchestrator.py:1693-1734/1847`。反证：`member_policy`/`input_hash`/传输 client 均非 live-profile 派生；workflow_spec 漂移仍被拦；下游阶段动作再受 `phase_protocol._next` profile 重校（漂移下即使收到审批也不会推进有害副作用）。故不构成越权/数据完整性/错投递。 | 记为 P3 改进项，不阻塞退出。**不建议**在此加 profile 阻断：`request_infoflow_approval` 本不消费 profile，加守卫属"为守卫而守卫"，且可能卡死超时门禁的合法 reissue 恢复（标准 §3"正常审批…不得被新守卫卡死"）。如需统一，应先固定"漂移期是否应停止对已授权门禁的重发"的语义再实施。 |

说明：探索子上下文初判 `wait_infoflow_approval`"无任何装饰器"；经核对该方法在 `orchestrator.py:1847` 带 `@guard_execution`，workflow_spec 漂移已受控，仅无 profile 重校——已并入 IR-01 一并说明，不单列。

## 结论与停止条件

- **C：本轮定向复审未发现新的确认 P0/P1/P2。** GAP-WF08 所列可写入口的 C 必要证据已按上表逐入口具备。是否将首次评审的 `C: INCOMPLETE` 收为 `PASS` 并关闭 `GAP-WF08`，属负责人 Tom 的接受决定；模型不代为批准。若接受，建议在 [首次退出评审](2026-09-22-exit-review.md) 与 [GAP-WF08 任务](2026-09-22-profile-guard-followup.md) 追加关闭标注并绑定本 commit。
- **M：未验证；P：未验证。** 976 项控制面测试不代替这两个层级；生产就绪仍需所选模型的 M 与目标环境的 P。
- 剩余项：IR-01（P3，改进清单）；注入-API 纪律与真实宿主锁/平台适配（P）。
- 停止：定向复审预算（一轮）用尽且无确认缺陷，本轮停止，不另开全仓评审。出现新的可验证 P0/P1、真实运行违约、回归失败或代码/合同/环境/模型变化时，按影响另行指定范围重开。
