# 套件评审记录 R11-C

**结论先行：本片 FAIL。确认 7 项 P2，未确认 P0/P1。均为当前代码或内存复现证实的缺陷，尚未修复，也没有负责人风险接受记录。**

## 基线与范围

- **标准**：TAD-REVIEW-EXIT / 1.0；SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`，已核对。
- **仓库/分支**：`tom-autodev-suite` / `mvp`；base_commit：全量；target_commit：`9b8daff8a7693a14a3c150625ed67a688ff2a381`。HEAD 匹配，评审前后工作区均无改动。
- **目标/范围**：控制面工程验收 C，全量评审第 1 轮、分片 C；仅检查指定 CLI、桥接入口及文档，必要时读取调用方。未继承 A/B 分片结论。
- **身份**：workflow `workflow-spec-v2`、evidence contract `v2`；workflow canonical hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`。`tom-autodev/SKILL.md` SHA-256：`d212a9d7d8e1ecbec2354497dae83b48dfd552ea2a40e2f2a5a767766f26da99`。schema 使用目标提交版本；profile 复现采用仓库测试夹具 `test_orchestrator.PROFILE`；未执行模型 producer prompt。
- **评审者/日期/环境**：Devin；具体模型版本未知；2026-11-02；Darwin arm64、Python 3.14.4。
- **负责人/预算**：tom；初审第 1 轮，修复→复核已用 0 轮，后续按标准默认最多 2 轮。
- **执行边界**：未修改仓库，未运行业务编译/测试，未调用真实平台；复现使用真实函数、内存 SQLite 和明确隔离的平台 Mock。

## 验收矩阵

| 场景 / WF 关联 | 证据、命令及实际结果 | 状态 |
|---|---|---|
| CLI 命令与选项存在性 | 捕获真实 `main()` 解析器：38 个子命令；37 组文档参数组合及 16 组补充组合，53/53 解析通过。仅证明语法存在，不代表执行语义正确 | 通过 |
| 成功回执与 CLI 分派，WF-10 | 真实重开及幂等重放已提交内存数据库，但 CLI 返回 1，见 R11C-001 | 失败 |
| 查询只读与漂移边界，WF-04/08 | `next` 改检查点；`context` 修改 ProducerJob，见 R11C-002/003 | 失败 |
| 注册、审批与恢复文档 | 需求级 preflight、历史 REJECT、重开示例及 re-pin 指引存在行为偏差，见 R11C-004～007 | 失败 |
| MVP 第 4 步删除一致性 | 指定入口 import AST 未发现指向删除模块的导入；退役命令均不再注册。恢复提示仍有残留，见 R11C-007 | 部分失败 |
| 单模块回归 | `python3 -B -m unittest discover -s tom-autodev/scripts/tests -p 'test_workflow_spec.py' -q`：29 tests，OK | 通过 |
| CLI 文件系统回归 | 同方式运行 `test_cli_operations.py`：34 个测试均因沙箱无法创建临时目录而在 setUp 报错，未形成产品行为验证；不计作代码缺陷 | 未验证 |
| 其余完整 WF 场景 | 本片只核对相关文档与入口；不替代 A/B 分片及完整 C 回归 | 未验证 |

## Findings

下文 `.py` 定位均相对 `tom-autodev/scripts/`。所有条目均为**确认、未修复、待处置**。

| ID / 严重级别 | 违反合同、文件:行 | 触发、预期/实际及影响 | 当前证据与反证核对 | 建议修法 |
|---|---|---|---|---|
| **R11C-001 / P2** 成功操作返回失败退出码 | WF-10；README.md:537 承诺重开及重复执行返回已有目标。`orchestrator.py:4033–4047` | 干净 source 执行 `restart-from SRC --execute --reason 重开`：source 已 STOPPED、target 已 INTAKE、lineage 已写入，JSON 为 `ok:true / RESTARTED`，但退出码为 **1**；重放 `RESTART_REPLAY` 仍为 1。shell `&&`、`check=True` 等会误判失败。 | 内存 SQLite 执行真实 restart/CAS 与 CLI 分派，复现两种结果。根因是成功码白名单遗漏；同因还影响 `CORRECTION_DELIVERED`、`CORRECTION_ALREADY_DELIVERED`、`ALREADY_REVISED`、`ALREADY_REBOUND`。**反证**：下层已成功提交，并非隐藏失败；读取 JSON 可绕行。 | 补齐成功/幂等码，增加 CLI 级“实际效果＋退出码”断言，避免只测下层返回值。 |
| **R11C-002 / P2** 文档中的只读 `next` 会推进检查点 | docs/OPERATIONS.md:34；`orchestrator.py:3538–3539`、`:505–520`、`:583–629` | 已有 `WORKSPACE(previous_state=REVIEW)`，且 T1 已评审未提交；执行 `cli.py next RUN`，预期仅查询，实际追加 `SKIPPED_SUBMIT_RECOVERED` 并进入 **SUBMIT**。普通 GRILL 查询也会写 action 缓存。 | 内存库复现事件数 **2→3**、状态 WORKSPACE→SUBMIT；另验证缓存 **0→1**。**反证**：`next(read_only=True)` 已能避免这些效果，但 CLI 未传该参数；恢复逻辑本身有意可写，不等于查询合同成立。 | CLI 查询传 `read_only=True`；worker 如需恢复，保留其显式可写路径。补查询前后状态不变测试。绕行可用 `status --json` 或显式只读 API。 |
| **R11C-003 / P2** `context` 在只读查询中修改 job，漂移时也不例外 | WF-08；README.md:382、docs/OPERATIONS.md:31；`agent_bridge.py:454–461` | run 已 workflow drift，存在过期的 `PENDING/RUNNING` ProducerJob；执行 `context RUN`，实际先把 job 改为 **EXPIRED、agent_id=NULL**，随后才返回 `decision_reason_code=WORKFLOW_SPEC_DRIFT`。 | 真实 `StateStore.expire_stale_producer_jobs` SQL（`state_store.py:1334–1353`）内存复现成立；SQLite `query_only=ON` 时，查询直接报 `attempt to write a readonly database`。**反证**：分类调用虽传 `read_only=True`，不能撤销此前 expiry 写入；现有只读测试未覆盖过期 agent lease。 | 将 expiry 处理移至受守卫的驱动入口；查询只投影过期状态，不落账。增加漂移及只读数据库回归。临时查看可用已验证无行变更的 `status --json`。 |
| **R11C-004 / P2** 新注册流程生成的需求级 profile 无法被 preflight 选中 | README.md:261–291；`orchestrator.py:667–677` | 新安装仅有向导生成的 `config/projects/bgw/BGW-1995.yaml`，没有旧式 `bgw.yaml`；按文档执行 `preflight bgw`，实际报 `PROJECT_NOT_READY / missing:["profile_file"]`，未执行任何平台探针。 | `profile_wizard.py:412–420` 保存需求级路径，而 preflight 固定读取旧项目级路径；解析器也无 card/profile 选择项。内存文件系统复现：需求级 loader 为 ready，preflight 仍报缺文件。**反证**：已有旧式配置不受影响；模拟补齐同内容旧式文件后，6 个成功 Mock 探针得到 READY。 | 增加明确 card/profile 参数并同步文档；多需求时不得静默选最近文件。补“仅需求级配置”回归。旧式配置副本可临时绕行，但不能当成正确需求配置已预检的证明。 |
| **R11C-005 / P2** 文档把历史 REJECT 描述为重做，实际会停止 run | tom-autodev/references/approval-policy.md:122–131、state-machine.md:40；tom-autodev/SKILL.md:254–259 | 当前 G2 等待审批，按兼容指引回复 `REJECT <id> 补回滚场景`；预期按意见原地重做，实际 watcher 调用 `stop`，run 进入 **STOPPED**。 | `approval_watch.py:417–422` 仅对 REVISE 调 `_revise`，REJECT 走 stop；真实 `_settle()` 内存复现为 stop 1 次、revise 0 次。`AgentBridge.continue_run` 也只自动处理 REVISE（`:349–355`）。**反证**：显式 `run_change.revise` 接受部分历史 REJECT，但 watcher 停止后已不在可修改阶段，不能兑现自动恢复承诺；“需修改/REVISE”分支可行。 | 统一说明：`REVISE/需修改` 才重做，`REJECT/停止/驳回` 为停止；明确历史人工兼容的适用前提，不改变停止决策的安全语义。 |
| **R11C-006 / P2** SKILL 重开指令漏掉执行模式 | tom-autodev/SKILL.md:263–275；`orchestrator.py:3140–3150`、`:3543–3558` | 按 SKILL 执行 `restart-from RUN --reason 修改仓库`，或漂移段的 `restart-from RUN`；文档描述为创建承接运行，实际均只调用 **restart.plan**，不会创建 target。 | 两个原样示例经真实解析/分派：plan 各调用一次，execute 均为 0。补 `--execute --reason ...` 后才进入 execute。**反证**：README.md:537 正确解释默认只读，因此功能存在且可绕行，但 agent 的直接操作指引仍是错模式。 | 两处改成“先预检和确认处置，再执行 `restart-from RUN --execute --reason ...`”，注明按需提供 CR 处置选项。 |
| **R11C-007 / P2** 当前恢复提示仍指向已删除的 re-pin/原地迁移 | README.md:522；docs/OPERATIONS.md:92–94；违反 docs/MVP_CONVERGENCE_PLAN.md:274、278 的删除一致性及 OPERATIONS.md:231 的新恢复合同 | `PROFILE_CONFLICT` 后按故障表执行“显式 re-pin”，或按接入说明“合法 re-pin 后重建 runtime”；当前无此操作，`repin-profile RUN` 被 parser 拒绝，退出 2；单纯重建 runtime 仍按原 hash 报冲突。 | 安全解析确认退役命令在构造 Orchestrator 前即 invalid choice；`orchestrator.py:1801–1806` 仍核对原 pin。**反证**：同文后部已有正确恢复路径；恢复原配置字节的内存复现为 `PROFILE_CONFLICT→OK`，故是错误指引而非要求恢复旧功能。 | 删除有效操作说明中的 re-pin/原地迁移选项，统一为恢复兼容文件，或核对未决远程对象后 `stop`＋`restart-from`。不要为修文档重新引入退役能力。 |

**明确排除的误报**：MVP 历史记录和删除说明不算悬空调用；`recover-skipped-submit:` 是内部幂等键；`_submit_companion` 是明确保留的正常 SUBMIT 实现；保留在策略 hash 中的旧标签不等于命令仍存在。未发现本片入口仍导入已删除模块。

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/理由 | 负责人及接受决定 | 到期/重开条件 |
|---|---|---|---|---|
| R11C-001～007 | CLI 结果判定、只读查询、预检及恢复指引 | 可行绕行已在上表说明；不等于缺陷被接受 | tom；**待决定** | 修复后按 ID 定向复核，或记录完整 P2 接受/延期决定 |
| LIM-C-01 | 文件系统型 CLI/bridge 回归缺证据 | 沙箱连 `/tmp` 都不可写；内存复现不替代该回归 | tom；待补证据 | 在可写隔离环境补单模块回归 |
| LIM-C-02 | 未合并 A/B、未跑完整 C；M/P 未验证 | 本片范围及禁止真实平台调用的边界 | tom；无通过决定 | 汇总其他分片及必要回归后再判完整 C；M/P 单独验收 |

## 结论

- **C：BLOCKED（本片）**。7 项确认 P2 未修复、未获明确接受；另有文件系统回归缺口。不能据本片结果宣称完整 C 通过。
- **M：未验证**；未执行固定模型行为资格场景。
- **P：未验证**；未调用真实平台，未继承历史发布记录作为本轮资格证据。
- **下一步**：按 finding ID 有界修复或作明确风险决定，补受影响回归，再仅复核修复及影响面。本片固定范围核验完成，停止扩展验收目标。

**本片结论：FAIL。**