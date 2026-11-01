**套件评审记录 R10／修复复核第 3 轮**

- 标准：`TAD-REVIEW-EXIT / 1.0`；已读取 `AGENTS.md:1`，核对标准 SHA-256 为 `5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库／分支：`tom-autodev-suite / mvp`；工作区干净。
- base_commit：`4e7417dc67eae613844c5478a92bb669795ccff8`
- target_commit：`ae3c5d938792966dccbc81b322ca6e44f2d9f3c5`
- 目标／范围：局部修复复核；指定差分的 5 个文件及 `_restart_build_status` 的调用方。环境：当前 macOS、Python 3.14.4。
- 规则身份：workflow canonical hash 为 `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；profile/schema/skill 规则相对 base 未变，以 target Git tree 固定；不涉及 prompt 资格验证。
- 评审者：Codex 独立上下文，精确模型版本不可见；日期：2026-11-02。负责人：评审发起人；本轮为负责人授权的第 3 轮、最后一轮。
- 继承证据：`docs/reviews/2026-11-02-r10-codex-review-2.md:1`，其 target 与本轮 base 一致；本轮重新复现唯一未关闭项，不直接继承其修复结论。

**验收矩阵**

| WF ID／场景 | 当前证据与实际结果 | 状态 |
|---|---|---|
| WF-04：无 build、profile 漂移、缺认证 | 同一内存状态调用真实 CLI `main()`：base 抛出 `IPIPE_AUTH_REQUIRED`；target 返回 `RESTART_READY`，transport 构造次数为 0、状态写入为 0。调用入口见 `tom-autodev/scripts/orchestrator.py:3612` | 通过 |
| WF-04：有 build、认证不可用 | 缺 token／登录凭据时返回 `RESTART_SOURCE_UNRESOLVED`，build 状态为 `None`；计划与 `--execute` 均未放行。HTTP 401 场景同样阻断。见 `tom-autodev/scripts/orchestrator.py:4951`、`tom-autodev/scripts/restart.py:244` | 通过 |
| WF-04、WF-08：正常查询及漂移守卫 | 两个已结束 build 分别按自身 module/revision/pipeline 查询，仅产生两个模拟 GET，计划返回 `RESTART_READY`；正常运行入口仍返回 `PROFILE_CONFLICT`，查询不写状态 | 通过 |
| WF-04：相邻恢复边界 | 缺 binding、运行中／UNKNOWN／被排除的 build 仍阻断；既有 CR 处置和已发布源 run 的守卫保持原结果 | 通过 |

验证方式：在 `tom-autodev/scripts/tests` 下，以 `python3 -B -c` 调用 unittest，使用内存 SQLite、虚拟 profile 和网络替身；**7 个选定现有用例＋7 个定向 CLI 场景全部通过，0 failures、0 errors**。未修改仓库文件，未运行业务项目编译／测试，未调用真实平台。

**Findings**

**新增确认 findings：无。**

| ID／严重级别 | 违反合同 | 失败场景及本轮实际结果 | 当前证据及反证核对 | 处置与修复／复核状态 |
|---|---|---|---|---|
| R10-NEW-001／原 P2 | WF-04；`docs/OPERATIONS.md:232` 的 profile 变更恢复约定 | 原场景为无 build、合法 profile H0→H1、缺认证，CLI 提前初始化 transport 而异常退出；本轮 target 已返回 `RESTART_READY` | `tom-autodev/scripts/orchestrator.py:4944` 先确认持久化 binding；`tom-autodev/scripts/orchestrator.py:4951` 才在 `try` 内初始化 transport。认证失败返回未知状态，下游 `tom-autodev/scripts/restart.py:244` 继续阻断，未将查询失败当作 build 已结束 | **关闭**。上一轮建议的“延迟初始化并纳入异常处理”已落实；原失败场景及有 build 的反向边界均已验证，无需追加修法 |

**剩余风险与改进**

本轮范围内无未关闭的确认缺陷，不新增改进要求。未复跑全量工程验收，也未验证模型资格或真实平台资格；未将 CHANGELOG 中的历史全量测试声明计作本轮执行证据。

**结论**

- C：**未验证全量**；本轮仅完成上述局部控制面复核。
- M：**未验证**；未运行固定模型行为集。
- P：**未验证**；网络使用替身，不代表真实平台验收。
- R10-NEW-001：**已关闭**；未发现修复引入的新确认缺陷。
- 停止原因：本轮必要条件已满足，且为负责人授权的最后一轮；停止本批次评审，仅在符合标准第 5 节的重开条件时重开。

**局部复核结论：PASS。**