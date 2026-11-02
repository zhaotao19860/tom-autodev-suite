**结论先行：分片 A 为 FAIL。确认 7 项缺陷：P1 × 4、P2 × 3；本片证据已构成 C 验收阻断。未发现 P0，不列风格建议。**

# 套件评审记录 R11-A

- 标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`，核对一致。
- 仓库/分支：`tom-autodev-suite / mvp`；base_commit：全量；target_commit：`9b8daff8a7693a14a3c150625ed67a688ff2a381`。HEAD 匹配，工作区无改动。
- 目标/范围：控制面工程验收 C，第 1 轮、分片 A；限定所列 14 个文件及必要调用方，检查审批绑定、门不可绕过、状态出口、漂移/profile 守卫及续跑 handoff。
- 规则身份：`workflow-spec-v2`，运行策略 hash 为 `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；schema、skill 均按上述 commit 内容核对；profile 使用测试 fixture，未使用真实项目配置。
- 评审者/日期：Codex 独立上下文，精确模型版本未知；2026-11-02。取证环境：Darwin arm64、Python 3.14.4。
- 负责人：本轮发起人，姓名未提供；预算：默认最多两轮“修复→复核”，已使用 0 轮。无继承的验收通过结论。
- 执行边界：未修改仓库、未运行业务编译/测试、未调用真实平台。沙箱连 `/tmp` 写入也受限，定向复现使用命令内脚本、内存 SQLite 和隔离 fixture，不据此声称磁盘崩溃恢复通过。

## 验收矩阵

| WF ID / 本片场景 | 证据、用例/命令、实际结果 | 状态 |
|---|---|---|
| WF-01：状态出口与修订重入 | WORKSPACE 的 REVISE 无消费出口；修复 PLAN 重做后丢失任务前沿，见 R11A-002、003 | 失败 |
| WF-02：准确授权、门不可绕过 | scope 临时批准越出 prepare 边界；旧输入的拒绝仍能停止新输入运行，见 R11A-001、004 | 失败 |
| WF-04：副作用恢复、审批 handoff | 纠正消息发生盲目重发；scope 批准不产生续跑 handoff，见 R11A-006、007 | 失败 |
| WF-08：漂移前置守卫 | Stop-hook 在检查漂移之前完成 handoff 并写审计，见 R11A-005；profile 守卫仅完成静态核对，未完整回归 | 失败；部分未验证 |
| WF-10：状态展示真实性 | 活跃 scope 审批被过滤，状态反称“审批记录尚未创建”，见 R11A-006 | 失败 |
| 相关正常路径回归 | `test_workflow_spec.py` 原样 29 项通过；`test_scope_approval.py` 内存 SQLite 适配后 19 项通过；`test_run_brief.py` 15 个只读 fixture 用例通过，另 2 个磁盘 CLI 用例未运行 | 已验证上述边界；非完整 C 回归 |

## Findings

以下均为**确认缺陷，未修复、未复核**，按严重度排序。

| ID / 严重级别 | 违反合同与文件:行 | 触发、预期/实际、影响 | 当前证据及反证核对 | 建议修法 |
|---|---|---|---|---|
| **R11A-001 / P1**：临时 scope Plan 被当作正式批准复用 | WF-02；prepare 临时批准不得越出 scope 边界。`tom-autodev/scripts/phase_protocol.py:1522`；`tom-autodev/scripts/scope_approval.py:78` | scope C 中，Plan 仅有 `scope-pending:C`，Review 失败，尚未产生/批准 C 卡。随后 G6 选择 `CODE_ONLY` 修复。预期先补足正式 Plan 授权；实际 `_accepted_plan` 将该 Plan 判为可复用，直接返回 IMPLEMENT，修复段又退出 scope，原临时 G4 不再被正式展开。 | 实测 `scope_card_count=0`、Plan 通道仍为 `scope-pending:C`，诊断目标却是 `IMPLEMENT/T1/OK`；修复后的 `segment(G5)`、`segment(G7)` 均为 `None`。反证：Plan 的 DAG/hash 检查只能证明身份一致，不能证明人已批准；后续 G6/G5/G7 不是缺失 G4 的替代授权。现有 scope 台账单测未覆盖 prepare 失败后复用。 | 复用时校验 Plan 批准来源：临时记录不能作为正式批准；所属 scope 未获批则回 PLAN，取得真实 G4，或重新完成受控 scope。 |
| **R11A-002 / P1**：WORKSPACE G4 接受无法消费的 REVISE | WF-01、WF-04。`tom-autodev/scripts/approval_ledger.py:419`；`tom-autodev/scripts/approval_delivery.py:250` | WORKSPACE 的 G4 卡显示“需修改”，用户点击后台账终结为 REVISE。预期能原地重做，或拒绝该不支持的决定；实际 WORKSPACE 不属于可修订状态，当前准确 hash 永久拿不到 APPROVE，该 run 无法继续。 | 实测依次得到：接收 `OK` → `revise` 返回 `REVISE_NOT_SUPPORTED` → 重发返回 `APPROVAL_NOT_TIMED_OUT` → 再回复同意仍保持 REVISE。反证：`tom-autodev/scripts/run_change.py:144` 明确拒绝 WORKSPACE，worker 又只消费 APPROVE；停止后新建 run 不等于恢复当前 run。 | 可修订性必须绑定审批对象/阶段，而非仅凭 G4 门号。WORKSPACE 未实现重做前，应同时隐藏按钮并在台账拒绝 REVISE，保持审批待决。 |
| **R11A-003 / P1**：修复 PLAN 重做丢失修复任务 | WF-01、WF-04。`tom-autodev/scripts/run_change.py:200`；`tom-autodev/scripts/phase_protocol.py:1131` | 支持的 `CODE_ONLY → PLAN` 回退中，T1 已有历史通过 Review。用户对新修复 Plan 提 REVISE。预期仍重做 T1；实际新事件只保存 revisions 与 reentry，丢掉修复前沿来源，任务解析重新将 T1 当作已完成；无其他任务时卡在 `TASK_FRONTIER_EMPTY`。 | 使用实际 StateStore/ApprovalLedger 内存库复现：修订前 task=T1，`revise=OK`，修订后事件键仅为 `reentry/source_revisions`，下一动作返回 `TASK_FRONTIER_EMPTY`。反证：代码对“来自 DIAGNOSE 的 PLAN”已有保留任务例外，但 REVISE 事件不再满足；现有 PLAN-revise 测试仅核对 revisions，未验证下一动作。 | 保留并验证修复任务及重入来源，让 `_task_id` 识别合法 PLAN-REVISE 链；仅补 `task_id` 仍可能命中“已 Review”跳过逻辑。 |
| **R11A-004 / P1**：旧输入的停止决定作用于新输入 | WF-02；绑定输入变化后旧审批应失效。`tom-autodev/scripts/approval_watch.py:403`；`tom-autodev/scripts/run_change.py:248` | 同一 IPIPE 检查点中，远程 stage 配置/参数变化，G8 从 hash A 改为 B；B 已批准，A 的旧卡仍未过期。用户回应旧卡 REJECT。预期只审计旧决定；实际 `superseded` 仅识别 reentry，判 A 非陈旧，watcher 直接将当前 run 停为 STOPPED。 | 实测：两个不同 hash 的 G8，B 为 APPROVE；处理 A 后 `superseded=false`，`stopped_state=STOPPED`。反证：台账检查的是回应与旧卡自身 hash 相符，不检查它是否仍是当前操作；worker 的准确 hash 守卫无法阻止 watcher 直接执行 stop。 | 旧卡失效判断同时绑定当前操作/输入，不只检查 reentry；状态操作前重新核验当前有效审批身份，失效回应仅审计。 |
| **R11A-005 / P2**：Stop-hook 漂移守卫之前已写状态 | WF-08。`tom-autodev/scripts/approval_watch.py:862`；`tom-autodev/scripts/approval_watch.py:881` | run 已发生 workflow 漂移，存在两条可清理的旧 handoff；无关会话触发 Stop。预期在任何写入前拒绝；实际先清理 handoff，再检查会话及漂移；候选被清空后甚至直接返回成功。 | 实际 StateStore 内存库复现：返回 `NO_RESUME_HANDOFF`，开放 handoff 从 2 变 0，新增 2 条完成审计。反证：已有 drift 测试只有单 handoff，未进入清理分支；相同漂移下 `worker_driver.resume` 能拒绝且保持状态不变。 | 将逐 run 的漂移/会话检查前移到清理之前，并保护 `reap_stale_resume_handoffs` 的写入边界。 |
| **R11A-006 / P2**：scope 卡不参与状态展示及续跑 handoff | WF-04、WF-10。`tom-autodev/scripts/run_brief.py:135`；`tom-autodev/scripts/approval_watch.py:261` | scope run 停在 SUBMIT，存在待批 `SCOPE:C`，当前内部出口为 G7。预期显示该 scope 审批，并在如流批准后生成续跑 handoff；实际按门号直接比较，将 `SCOPE:C` 过滤，批准后也匹配不到 G7。D 卡同类。 | 实际台账复现：待批时 `waiting_on=[]`、状态“待处理”、提示“审批记录尚未创建”；如流批准后 `_handoff()` 返回空，未创建 resume handoff。反证：手动再 continue 可经 `_scope_request` 展开批准，已验证出口 G7 得到正式 APPROVE，所以并非授权丢失，但自动续跑信号和状态错误。 | 根据 scope 中绑定的准确出口输入，将 scope 卡映射到当前 gate；状态与 handoff 共用同一验证逻辑，不做宽泛门号别名。 |
| **R11A-007 / P2**：纠正消息将发送后 TypeError 当作未发送 | WF-04。`tom-autodev/scripts/approval_delivery.py:83` | G6 中文纠正已被远端接收，客户端在处理响应时抛 TypeError。预期保留 UNKNOWN intent、先核对；实际仅凭异常类型撤回 intent，声明发送前拒绝，允许再次发送。 | 模拟客户端“记录发送后抛 TypeError”：两次调用都返回 `CORRECTION_REJECTED`，首次后 pending intent 为空，远程发送计数为 2。反证：普通审批投递已有“函数体内 TypeError 保持未知、不重发”的回归，但纠正入口使用另一套异常判断。 | 调用前单独检查签名；进入发送调用后的 TypeError 按未知结果处理。只有明确的 `REJECTED_BEFORE_SEND` 证据才允许撤回。 |

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/理由 | 负责人及接受决定证据 | 到期/重开条件 |
|---|---|---|---|---|
| R11A-001～004 | 授权边界或正常运行不可继续，阻断 C | 不以风险接受代替修复 | 修复负责人待发起人指定 | 修复后定向复核失败触发及正常路径 |
| R11A-005 | 漂移时修改 handoff | 暂避 Stop-hook 清理路径；受守卫的 resume 拒绝且不写入已验证 | 待决定，未接受 | 修复或明确延期；期限待负责人指定 |
| R11A-006 | scope 状态及自动续跑缺失 | 手动 continue；scope 展开出口已验证 | 待决定，未接受 | 修复或明确延期；期限待负责人指定 |
| R11A-007 | 展示纠正消息可能重复发送 | 暂停纠正入口，保留原审批卡；其 ID/hash 不受纠正操作改变 | 待决定，未接受 | 修复或明确延期；期限待负责人指定 |
| 验证限制 | 磁盘持久化、完整套件回归、其他分片及真实平台 | 不将内存复现或局部全绿外推为完整通过 | 待合并验收证据 | 可写隔离环境及跨片证据齐备后补验 |

## 结论

- **C：BLOCKED**，本片存在 4 项未解决 P1；另有 3 项尚未获接受的 P2。此结论不是 A/B/C 合并后的完整验收报告。
- **M：未验证；P：未验证。**
- 未验证项：磁盘崩溃恢复、完整必要回归及范围外矩阵；后续应修复确认项，再按默认预算定向复核，不自动扩大本轮目标。
- 停止原因：本片第 1 轮独立初审完成；仓库无修改，`git diff --check` 通过。

**本片结论：FAIL。**