**结论先行：本片 FAIL；C：BLOCKED。上一轮遗留 3 项中，2 项关闭，R11A-003 仍有同根因残留（P1）。未确认本轮相关 diff 引入的其他独立新缺陷。**

## 套件评审记录 R11-3A

已读取 `AGENTS.md:1` 与验收标准。标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256 核对一致：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。

仓库/分支：`tom-autodev-suite / mvp`，HEAD 匹配目标提交，工作区干净。
- base_commit：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`
- target_commit：`e41578cdddc09f013844c83acd2aceccd3930559`

目标：C 全量评审第 3 轮、分片 A 的最后一次修复复核；范围为 `run_change.py`、`run_brief.py`、`approval_watch.py` 的修复差异、相关测试及必要调用方。环境：Darwin arm64、Python 3.14.4。评审者：Codex 独立上下文，精确模型版本未知；日期：2026-11-02。负责人：本轮发起人，姓名未提供；已用完两轮“修复→复核”额度。

规则身份：`workflow-spec-v2`，策略 hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；schema/skill/prompt 按目标提交快照，profile 使用隔离 fixture。继承 `docs/reviews/2026-11-02-r11-codex-review-2A.md:1` 的合同与已关闭项证据；R11A-004 因相关代码再次修改，另行重放其原场景，不继承未经核验的全量通过结论。

## 验收矩阵与关闭台账

| Finding / 不变量 | 本轮复现与实际结果 | 状态 |
|---|---|---|
| **R11A-003 / WF-01、04** | base 在第 2 次 PLAN REVISE 后返回 `TASK_FRONTIER_EMPTY`；target 第 2 次已返回 `OK/T1`。但同一合法修订链到第 **33** 次，仍返回 `TASK_FRONTIER_EMPTY`，见下述残留证据 | **未关闭，部分修复** |
| **R11A-006 / WF-04、10** | 分别重放 C、D：待批均显示 1 条审批；批准后均显示“已批准待提交”，不再提示“审批记录尚未创建”；各生成 1 条续跑 handoff。base 的批准后展示错误可复现 | **关闭** |
| **R11-001 / WF-02** | 两个独立 stage 的 G8 并存，B 已批准后在 A 卡回复“停止”：base 保持 IPIPE；target 判 A 未被替代，实际转为 STOPPED，随后 `next=TERMINAL_STATE`。反证回归：同一 stage 输入 A→B 后，旧 A 的停止仍仅审计，不停止当前运行 | **关闭** |

定向回归：既有 **89 项通过**——workflow 29、纠正消息 2、只读 brief fixture 15；内存存储适配后的 watcher 28、scope worker 12、run-change 选定用例 3。另有 **1 项内联边界回归失败**。`git diff --check 01f123a..e41578c` 通过。

## Findings

| ID / 严重级别 | 违反合同与定位 | 失败场景、当前证据及反证核对 | 处置与建议修法 |
|---|---|---|---|
| **R11A-003 / P1，同根因残留** | WF-01、04；修订应留在同一修复任务。`tom-autodev/scripts/run_change.py:210`；`tom-autodev/scripts/phase_protocol.py:1114` | 已通过 Review 的唯一任务 T1 经 DIAGNOSE 返回 PLAN，连续执行 33 次合法 REVISE。使用真实内存 ProducerJob、审批台账和审批哈希重放：前 32 次 `next=OK/T1`；第 33 次事件仍保存 `task_id=T1`、有效 `repair_of`，但链检查仅回溯 32 层，误判不是修复重入，继而因 T1 已 Review 得到 `TASK_FRONTIER_EMPTY`。新增 unittest 实际失败。任务依赖已满足，原“两次修订”测试通过，不能排除此残留；`tom-autodev/references/approval-policy.md:124` 未规定 32 次 REVISE 上限 | **确认、未关闭**，保留原 ID。将 `repair_of` 固定指向经验证的诊断重入根事件，或使用带循环检测的完整合法链遍历，避免把深度截断解释成“非修复”；补超过 32 次修订后仍能取得 T1 的回归 |

## 剩余风险与限制

| Finding / 限制 | 影响与处置 | 负责人、接受决定及停止条件 |
|---|---|---|
| R11A-003 | 合法修订链仍可使当前修复任务无法继续，P1 阻断退出 | 修复负责人待发起人指定；不能以预算耗尽代替修复。转为明确修复任务，不自动追加评审轮次 |
| 取证限制 | 沙箱禁止 `/tmp` 写入，临时逻辑均内联执行，SQLite 使用内存；未验证磁盘恢复、2 个磁盘 CLI 用例或完整套件 | 未修改仓库、未运行业务编译/测试、未调用真实平台；不将局部结果提升为全量 C 或 M/P 通过 |

## 结论

**C：BLOCKED（本片存在确认 P1）；M：未验证；P：未验证。** 最后授权复核轮已完成，按第 5 节停止开放式搜索；剩余问题交负责人作明确修复任务处理。

**本片结论：FAIL。**