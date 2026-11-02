# 套件评审记录 R11-C／第 2 轮

**结论先行：本片 FAIL。R11C-001～007 的原失败场景均已关闭；修复影响面另确认 1 项 P1、1 项 P2，尚未解决。P1 是共享修复对连续 REVISE 处理不完整，汇总时应与 R11A-003 去重。**

## 基线与范围

- **标准与代码**：已读 `AGENTS.md:1`、`docs/WORKFLOW_EXIT_CRITERIA.md:1`。标准 `TAD-REVIEW-EXIT / 1.0`，SHA-256 核对为 `5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。仓库 `tom-autodev-suite`，分支 `mvp`；base_commit：`9b8daff8a7693a14a3c150625ed67a688ff2a381`；target_commit：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`，HEAD 匹配。
- **规则身份**：workflow `workflow-spec-v2`，canonical hash `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；evidence contract `v2`；schema Git tree `b009dbae263cafb7ef1b4c07ddf69fc8130b358a`。`tom-autodev/SKILL.md:1` SHA-256：`3d92c2131331cfc9bc987dcad2dae2357947b1602966b83e584560ece19d4f72`。profile 使用 `test_orchestrator.PROFILE`，复现字节 SHA-256：`835c9a7ed531af2720c9c49491826d63d542cd8a7422be45bbec0f170551aa30`；未执行模型 producer prompt。
- **范围与责任**：C 第 2 轮、分片 C，仅复核旧 finding、修复 diff 及相关入口的安全边界；来源为 `docs/reviews/2026-11-02-r11-codex-review-1C.md:1`，不继承 A/B 的通过结论。评审者 Codex，具体模型版本未知；日期 2026-11-02；环境 Darwin arm64、Python 3.14.4。负责人 tom；修复→复核预算已用 **1/2**。

## 验收矩阵：旧 Finding 逐项复核

复现使用真实控制器、真实台账逻辑、内存 SQLite及隔离的平台 Mock；文件系统限制见风险台账。

| ID／关联合同 | 原失败场景在 target 的复核证据 | 状态 |
|---|---|---|
| R11C-001／WF-10 | 真实 CLI 执行重开：`RESTARTED`、退出 **0**，源 STOPPED、目标 INTAKE、lineage 已提交；重复执行 `RESTART_REPLAY`、退出 **0**，目标相同。成功码见 `tom-autodev/scripts/orchestrator.py:4060`。 | **关闭** |
| R11C-002／WF-04、08 | WORKSPACE、上一任务已通过 Review 的原夹具，在 `query_only=ON` 下执行 CLI `next`：事件 **2→2**、数据库 dump 相同；GRILL 查询缓存 **0→0**。可写控制组仍能恢复 SUBMIT，证明夹具确实覆盖旧触发。入口见 `tom-autodev/scripts/orchestrator.py:3551`。 | **关闭** |
| R11C-003／WF-08 | 过期 agent 租约下调用 `context`：正常查询及 workflow 漂移阻断均不改变数据库，原 RUNNING／agent 身份保留；随后正常 claim 可接管过期租约。见 `tom-autodev/scripts/agent_bridge.py:454`。 | **关闭** |
| R11C-004／WF-10 | **仅存在需求级 profile**、没有 legacy 文件时，真实 CLI `preflight bgw --card BGW-1995` 返回 **READY／0**，六个 Mock 探针各执行一次，数据库不变。见 `tom-autodev/scripts/orchestrator.py:659`。缺失／非法 card 的另一问题另列。 | **原触发关闭** |
| R11C-005／审批合同 | G2 回复 `REJECT <id> 补回滚场景`，真实 watcher 结算后 STOPPED、revise 调用 0 次；REVISE 分派重做一次。文档已明确两者区别，见 `tom-autodev/references/approval-policy.md:124`。 | **关闭** |
| R11C-006／恢复合同 | `tom-autodev/SKILL.md:264` 已区分只读 plan 与 `--execute --reason`。按新指令执行，漂移源运行成功重开，退出 0，新运行钉住当前 workflow hash。 | **关闭** |
| R11C-007／WF-08 | 修改 profile 字节触发 `PROFILE_CONFLICT`，恢复原字节后正常；新运行重开路径成功。退役 `repin-profile` 仍在构造控制器前被 parser 拒绝，退出 2。有效指引已改正，见 `README.md:524`、`docs/OPERATIONS.md:94`。 | **关闭** |
| 安全边界与回归抽查／WF-02、04、07、08 | 错审批 hash 被拒；纠正消息发送后抛 TypeError，连续调用均 `QUERY_REQUIRED`，**仅发送一次且保留 intent**。重开发现 RUNNING build／查询异常均阻断，SUCCESS 才 READY，查询不写库。发布正例、多 release 歧义、异模块／异 build 证据三个测试方法以内存 setup 执行通过。原样 unittest：workflow **29**、CLI 退出码 **1**、approval correction **2**，均 OK；固定提交 `git diff --check` 退出 0。 | **仅上述场景通过；不代表完整 WF 矩阵通过** |

## Findings

| ID／严重级别 | 违反合同与文件:行 | 失败场景、当前证据及反证核对 | 建议修法与处置 |
|---|---|---|---|
| **R11-C-001／P1** 连续 REVISE 丢失修复任务绑定 | WF-03、WF-09；`tom-autodev/scripts/run_change.py:204`，下游 `tom-autodev/scripts/phase_protocol.py:1149` | T-1 已通过 Review，T-2 依赖 T-1；诊断后进入修复 PLAN(T-1)，连续两次合法“需修改”。预期始终重做 T-1；实际第一次仍 T-1，**第二次真实 `next` 返回 OK／PLAN／T-2**，修复对象被静默替换。两轮草案内容校验、审批结算、revise 均为 OK；取证未 Mock `_current_draft` 或审批 hash 计算。根因是新增保存逻辑只认 `previous_state=DIAGNOSE`，第一次 REVISE 后该字段已不存在，第二次便丢掉 `task_id/repair_of`。**反证**：新增链识别函数及单次 REVISE 回归只能保住第一次，无法恢复第二次已丢失的字段。 | **确认、未关闭**。这是 R11A-003 共享修复未完整覆盖的连续操作，汇总去重。对经校验的修复重做链继续保留准确 `task_id/repair_of`，不要泛化复制旧事件；增加连续两次以上 REVISE 的真实台账回归。 |
| **R11-C-002／P2** 显式 card 预检静默回退 legacy | `README.md:294` 承诺预检该需求 profile；WF-10；`tom-autodev/scripts/orchestrator.py:672` 调用 `tom-autodev/scripts/project_registry.py:74` | 仅有有效 `projects/bgw.yaml`，没有对应需求文件。真实 CLI 分别传 `--card BGW-1995`、`--card ../wrong`，均加载 **legacy `bgw.yaml`**，返回 **READY／退出 0**。helper 对需求文件缺失回退 legacy，还吞掉非法 card 的 ValueError，外层异常处理无效。**反证**：需求级文件存在的正例已通过；此处不直接产生写入，因此按局部预检缺陷定 P2，不夸大为越权。 | **确认、未关闭**。显式 card 应校验后直读准确需求路径；缺失／非法时拒绝，不执行探针。只有省略 card 才读 legacy。增加“legacy 有效但指定需求文件缺失／card 非法”回归。 |

## 剩余风险与改进

| ID | 范围、绕行与负责人决定 | 到期／重开条件 |
|---|---|---|
| R11-C-001、002 | tom；均未有接受决定。P1 必须修复验证，不能接受放行；P2 可暂用已验证的准确需求级文件预检，但绕行不等于接受。 | 修复后的固定 commit 定向复核；P2 若延期，须补理由、负责人、绕行及到期条件。 |
| LIM-C | 沙箱连 `/tmp` 也禁止写入，故临时复现改用 stdin，**未落盘脚本，文件系统型回归仍缺证据**。本轮未修改仓库、未运行业务编译／测试、未调用真实平台。结束时检测到 `tom-autodev/scripts/clients/icode_runtime.py:421`、`tom-autodev/scripts/clients/ipipe_runtime.py:2382` 出现非本轮写入的未提交改动，已排除；受测发布函数代码对象与固定 target 源码编译结果一致。tom 待补证据。 | 在可写隔离环境补受影响回归；未提交修复须另固定 commit，不能挪用本报告结论。 |

## 结论

**C：BLOCKED（本片）**，依据为上述 P1、未处置 P2；完整 C 回归和其他分片未在本轮完成。**M：未验证；P：未验证。**

按 §5，本轮停止扩展范围；后续只处理上述确认项及受影响回归，不重新开放全仓扫描。

**本片结论：FAIL。**