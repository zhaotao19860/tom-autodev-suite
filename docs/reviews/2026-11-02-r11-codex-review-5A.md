**结论先行：R11A-003（P1）已关闭。上一轮失败在 base 可复现，在 target 均已修复；未发现本项相关 diff 引入的新缺陷。**

## 套件评审记录：第 5 轮，分片 A

- 标准：已读 `AGENTS.md`；`TAD-REVIEW-EXIT / 1.0`，SHA-256 核对一致：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库/分支：`tom-autodev-suite / mvp`；HEAD 匹配 target，工作区干净。
- base_commit：`cf27f02d3e975932483d46fa14d229cae717cc6f`
- target_commit：`52128a7ed2f498863e29733c4eaa24a2324d0852`
- 范围：仅 R11A-003 的 `phase_protocol.py`、`run_change.py`、对应测试差分及调用链；Darwin arm64、Python 3.14.4、SQLite 3.53.0。
- 规则身份：`workflow-spec-v2`，hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；schema/skill/prompt 按 target 快照，profile、TASKS/REVIEW 使用隔离 fixture。
- 评审者：Codex 独立上下文，精确型号未知；日期：2026-11-02；负责人：发起人，姓名未提供。按用户指定执行第 5 轮一次定向复核，不自动续轮。
- 继承证据：`docs/reviews/2026-11-02-r11-codex-review-4A.md:1` 的失败触发，本轮重新执行取证。

## 验收矩阵

内联复现使用真实控制器入口、ProducerJob、审批哈希及 SQLite 台账；未调用平台。

| 不变量 / 场景 | 实际结果 | 状态 |
|---|---|---|
| WF-01、04：新格式长链 | 单任务、多任务各连续重做 **100 次**，始终 `OK/T1`，新 `repair_of` 均直指诊断重入根；期间共 20 次 SQLite 重载 | 通过 |
| WF-01、04、08：旧格式升级 | **2/3/32/40 层 × 单/多任务**：同输入在 base 分别返回 `TASK_FRONTIER_EMPTY` / `OK/T2`，继续重做均为 `APPROVAL_STALE`；target 全部恢复 `OK/T1`，审批仍可消费，后续重做统一指根；升级前后共 16 次重载 | 通过 |
| 身份与循环守卫 | 错任务、缺来源、非 PLAN 来源、普通 PLAN 来源、错误 reentry、自环、双节点环、尾链入环，均不认定为修复重做；8 类均再经 SQLite 重载验证 | 通过 |
| 无深度截断 | 额外构造 **10,000 层**旧格式链，准确返回根事件；循环不会无限回溯 | 通过 |
| 正常路径及相关回归 | 普通 PLAN 重做 40 次通过；修复身份不绕过任务依赖；相关 unittest **4/4**、相关 `git diff --check` 通过 | 通过 |

四个 unittest 仅跳过创建磁盘 fixture 的 `setUp`，测试体均为独立 Mock 用例；不声称整个模块通过。

## Findings

| ID / 严重级别 | 违反合同 | 原失败与当前证据、反证核对 | 处置 |
|---|---|---|---|
| R11A-003 / P1 | WF-01、04、08：修复任务连续重做及历史检查点恢复不得丢失任务身份 | `tom-autodev/scripts/phase_protocol.py:3090` 按 `repair_of` 迭代回溯并检测循环，无任意深度上限；`tom-autodev/scripts/phase_protocol.py:1112` 核对根任务身份；`tom-autodev/scripts/run_change.py:214` 将后续重做归一到根。上述 base 失败/target 成功对照成立，审批、版本及依赖守卫未被绕过 | **关闭** |

无新增 `R11-` finding；停止扩大搜索范围。

## 剩余风险与改进

| 限制 | 范围与处置 | 负责人 / 后续条件 |
|---|---|---|
| 只读取证边界 | 共 **44 次 SQLite 序列化→新内存连接恢复**；未覆盖磁盘 I/O、全量 C 或 M/P。不修改仓库、不运行业务编译/测试、不调用真实平台 | 发起人；扩大验收范围时另行取证，不视为风险接受 |

## 结论

- C：未验证全量；M：未验证；P：未验证。
- 局部复核：R11A-003 已关闭，本片无遗留确认缺陷；按第 5 节停止本次定向复核。

**本片结论：PASS。**