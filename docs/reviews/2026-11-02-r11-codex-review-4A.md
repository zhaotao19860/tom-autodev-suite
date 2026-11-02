**结论先行：FAIL。R11A-003（P1）未关闭。** 第 33 次重做丢任务已修复，但相关 diff 引入旧修订链恢复回归：原本可继续的修复任务，升级后被丢弃或误切到下一任务。按同根因合并规则，沿用原 ID。

## 套件评审记录：分片 A

- 标准：`TAD-REVIEW-EXIT / 1.0`；已读 `AGENTS.md`，标准 SHA-256 核对一致：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库/分支：`tom-autodev-suite / mvp`；HEAD 匹配 target，工作区干净。
- base_commit：`e41578cdddc09f013844c83acd2aceccd3930559`
- target_commit：`cf27f02d3e975932483d46fa14d229cae717cc6f`
- 范围：仅 R11A-003 的 `run_change.py`、`phase_protocol.py` 相关差分、调用方与回归；环境为 Darwin arm64、Python 3.14.4。
- 规则身份：`workflow-spec-v2`，hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；schema/skill/prompt 按 target 快照，profile 使用隔离 fixture。
- 评审者：Codex 独立上下文，精确模型版本未知；日期：2026-11-02。负责人：发起人，姓名未提供。
- 继承证据：`docs/reviews/2026-11-02-r11-codex-review-3A.md:29` 的失败场景，本次重新执行验证。原批次预算已耗尽；本次仅执行用户明确指定的一次定向复核，不自动续轮。

## 验收矩阵

| 不变量 / 场景 | 实际证据与结果 | 状态 |
|---|---|---|
| WF-01、04：连续重做 | 真实 ProducerJob、审批哈希及台账重放：base 第 33 次返回 `TASK_FRONTIER_EMPTY`；target 连续 100 次均为 `OK/T1`，始终直指诊断重入根事件 | 原触发已修复 |
| WF-04、08：旧检查点恢复 | base 合法重做 2、3、32 次后交 target 解析，均变为 `TASK_FRONTIER_EMPTY`；继续 `revise` 返回 `APPROVAL_STALE`。SQLite 序列化→新内存连接恢复后同样复现 | **失败** |
| WF-01：多任务变体 | 旧链重做两次后，base 返回 `OK/T1`，target 误返回 `OK/T2`；新链连续 100 次不误切任务 | **失败** |
| 相关正常路径与守卫 | 普通 PLAN 重做 40 次、target 100 次重做期间 10 次 SQLite 恢复、无效来源/任务不匹配/依赖守卫均通过；既有相关 unittest 2 项通过，相关 `diff --check` 通过 | 通过 |

## Findings

| ID / 严重级别 | 违反合同与定位 | 失败场景、证据及反证核对 | 处置与建议 |
|---|---|---|---|
| **R11A-003 / P1** | WF-01、04、08；`tom-autodev/scripts/phase_protocol.py:1112`；`tom-autodev/scripts/run_change.py:212` | base 生成 `P0 ← redo1 ← redo2` 的合法持久化链。target 只查一次，发现 `redo1` 不是直接从 DIAGNOSE 进入，便否认修复身份，随后跳过已 Review 的 T1。相同断言的三项恢复回归：**base 3/3 通过，target 3/3 失败**。ProducerJob 仍为 `FULFILLED`、审批仍为 `REVISE`，workflow 无漂移，不能由上游守卫排除此场景。`tom-autodev/scripts/tests/test_run_change.py:230` 只覆盖已经扁平化的链，未覆盖旧格式恢复 | **未关闭，同根因恢复变体**。保留新格式快路径；旧链采用带循环检测、无任意深度截断的合法回溯，后续重做统一指向根事件；补旧链恢复及多任务回归 |

未确认其他独立新 finding；上述恢复回归并入原项，不重复立项。

## 剩余风险与改进

| 限制 / 风险 | 影响与处置 | 负责人及重开条件 |
|---|---|---|
| R11A-003 | 旧运行可能卡死或误切任务；P1 不可接受风险放行 | 发起人指定修复负责人；修复后仅复核本项 |
| 取证边界 | 沙箱只读，脚本内联、SQLite 全在内存；未验证磁盘 I/O、全量 C 或 M/P | 未修改仓库、未运行业务编译/测试、未调用真实平台 |

## 结论

- **C：BLOCKED（本片存在确认 P1，不是全量 C 结论）；M：未验证；P：未验证。**
- 停止扩展搜索，转为明确修复任务。

**本片结论：FAIL。**