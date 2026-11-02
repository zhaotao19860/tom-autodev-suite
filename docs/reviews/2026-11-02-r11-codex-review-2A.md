**结论先行：本片 FAIL，C：BLOCKED。上一轮 7 项中，5 项原失败场景关闭、2 项未关闭；另确认新增 1 项 P1。当前剩余为 P1 × 2、P2 × 1。**

## 套件评审记录 R11-2A

标准：`TAD-REVIEW-EXIT / 1.0`；已读取 `AGENTS.md:1`，标准 SHA-256 核对一致：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。

仓库/分支：`tom-autodev-suite / mvp`；base_commit：`9b8daff8a7693a14a3c150625ed67a688ff2a381`；target_commit：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`，HEAD 匹配。范围仅为 A 片七项修复及相关 diff、直接调用方和指定安全边界，不重扫全仓。

规则身份：`workflow-spec-v2`，策略 hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；schema/skill/prompt 按目标 commit 快照，profile 使用隔离 fixture。环境：Darwin arm64、Python 3.14.4。评审者：Codex 独立评审上下文，精确模型版本未知；日期：2026-11-02；负责人：本轮发起人，姓名未提供。本次为总评审第 2 轮、默认两次额度中的第 1 次修复复核。

继承证据：`docs/reviews/2026-11-02-r11-codex-review-1A.md:1` 的合同与失败触发，并核对 `CHANGELOG.md:7` 的修复声明；不继承全量通过结论。

## 验收矩阵

| 场景 | 本轮证据 | 结果 |
|---|---|---|
| WF-01、WF-02：修订出口、准确审批绑定 | 七项旧场景重放；连续 REVISE 仍丢任务，独立 G8 卡被误判替换 | **失败**，见 R11A-003、R11-001 |
| WF-04、WF-08、WF-10：未知写、漂移守卫、状态真实性 | 纠正消息不再盲重发；漂移前不清理 handoff；scope 批准后展示仍错误 | 前两项定向通过；WF-10 **失败** |
| 相关正常路径与安全回归 | `tests.test_workflow_spec` 原样 29 项、`tests.test_approval_correction` 原样 2 项通过；`tests.test_ipipe_runtime` 内存存储适配后 6 个选定用例通过，覆盖分支、发布身份/歧义及未知写不重放。SPEC 场景的 `next/status/context` 含过期 producer 租约，库快照不变、修改 SQL 为 0 | 仅上述边界通过，不代表完整 C |

## Findings

以下旧项均用原失败触发重放，并核对受影响路径；同根因残留保留原 ID。

| ID / 严重级别 | 违反合同与定位 | 触发、预期/实际 | 当前证据及反证核对 | 处置与复核状态 |
|---|---|---|---|---|
| **R11A-001 / P1** | WF-02；`tom-autodev/scripts/phase_protocol.py:1546` | scope C 尚无正式卡，Plan 仅为 `scope-pending:C`，Review 失败后选择 CODE_ONLY | base 返回 `IMPLEMENT/T1`；target 返回 `PLAN/T1`，不再复用临时批准 | **原场景关闭** |
| **R11A-002 / P1** | WF-01、04；`tom-autodev/scripts/approval_ledger.py:436` | WORKSPACE 的 G4 收到 REVISE，不应接受无法消费的决定 | 返回 `APPROVAL_REVISE_NOT_SUPPORTED`，有效决定仍为空；随后 APPROVE 成功。投递入口同步按状态限制选项 | **关闭** |
| **R11A-003 / P1** | WF-01、04；`tom-autodev/scripts/run_change.py:204` | 修复 PLAN 首次 REVISE 后应继续保留同一任务；连续修订也必须成立 | 首次已能返回 `T1/OK`；**第二次 REVISE 又丢失 `task_id/repair_of`，`next=TASK_FRONTIER_EMPTY`**。守卫只识别当前 payload 的 `previous_state=DIAGNOSE`；已有新增测试只覆盖一次 | **未关闭，部分修复**。沿已验证的修复 REVISE 链保留任务和来源，补连续两次 REVISE→next 回归 |
| **R11A-004 / P1** | WF-02；`tom-autodev/scripts/approval_watch.py:404` | 同一操作输入 A→B，B 已批准后旧 A 收到 REJECT | base 将 run 停为 STOPPED；target 保持 IPIPE，旧决定只审计 | **原场景关闭**；修法引入的独立操作误替换另列 R11-001 |
| **R11A-005 / P2** | WF-08；`tom-autodev/scripts/approval_watch.py:882` | workflow 漂移、两条可清理旧 handoff，无关会话触发 Stop-hook | base 开放 handoff `2→0`、新增两条完成审计；target 保持 `2→2`、完成审计为 0 | **关闭** |
| **R11A-006 / P2** | WF-04、10；`tom-autodev/scripts/run_brief.py:190` | C/D scope 卡须在待批及批准后准确展示 | 待批已从 `waiting_on=0→1`，批准后已生成 1 条续跑 handoff；但状态仍为“待处理”，并提示 **“审批记录尚未创建”**。批准分支仍只比较裸门号，漏掉 `SCOPE:C/D` | **未关闭，部分修复**。统一使用准确 scope/gates/hash 匹配待批、已批和 handoff，补批准后展示回归 |
| **R11A-007 / P2** | WF-04；`tom-autodev/scripts/approval_delivery.py:94` | 发送已发生后客户端抛 TypeError，不能声明未发送并重试 | 两次调用均为 `QUERY_REQUIRED`，模拟远端发送只有 1 次，纠正 intent 保持 pending；错误签名仍在 claim 前拒绝 | **关闭** |
| **R11-001 / P1，新增** | WF-02及“停止本 run”承诺；`tom-autodev/scripts/approval_watch.py:581` | 两个独立失败 stage A/B 分别申请合法 G8，B 已批准；用户在仍有效的 A 卡回复“停止”。预期停止 run，实际被当作旧卡忽略 | A 当前 hash **仍等于卡上 hash**，但 `_replaced(A)=true`；决定记为 REJECT，`stop_effect=null`，run 仍为 IPIPE。随后 B 实际触发 **1 次 FakeApi 外部写调用**，返回 `RERUN_CONFIRMATION_REQUIRED`。B 的准确 hash 守卫不能补救被 watcher 忽略的全局停止 | **确认、未修复**。按同一操作对象的 operation/module/pipeline/build/stage 等身份确认替换关系，不能仅凭同门、不同 hash、较新时间；补独立 G8 并存时停止任一卡的回归 |

## 剩余风险与改进

| ID / 限制 | 影响与绕行 | 负责人及接受决定 | 下一步 / 重开条件 |
|---|---|---|---|
| R11A-003、R11-001；R11A-006 | 两项 P1 阻断退出；P2 可人工核对真实批准台账及已建 handoff，但展示仍不可信 | 本轮发起人指定修复负责人；**未收到 P2 接受决定** | P1 修复验证；P2 修复或明确接受并记录期限，再做定向复核 |
| 取证与基线限制 | 沙箱禁止 `/tmp` 写入，故脚本内联执行、SQLite 使用内存，未验证磁盘崩溃恢复或完整套件。收尾发现 `tom-autodev/scripts/clients/icode_runtime.py:423` 出现目标外未提交改动，已排除；A 片取证源码仍与 target 一致 | 不将这些缺口视为通过 | 可写隔离环境补证；其他分片另行合并 |

## 结论

**C：BLOCKED（仅本片证据，不能替代合并全量验收）；M：未验证；P：未验证。** 本次未修改仓库、未运行业务编译/测试、未调用真实平台；固定提交间 `git diff --check` 通过。第 1 次修复复核完成，停止本片开放式搜索；后续只处理确认项及其回归。

**本片结论：FAIL。**