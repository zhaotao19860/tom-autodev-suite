# tom-autodev MVP 收敛方案

| 版本 | 日期 | 依据 |
|---|---|---|
| v1 | 2026-10-25 | 基线 `64d41e7`；11 个真实 run 无一到达发布 |
| v2 | 2026-10-27 | BGW-1995（run `160c6840`）第一次真实走进发布段，发布路径修了 5 个控制面问题 |
| v3 | 2026-10-27 | Codex 静态评审（基线 `2913d3a`）：改为"先投影、补合同、真实验证、最后删除" |

当前 `HEAD` 为 `2913d3a`。本文件是对"现状代码 + 方案设计"的计划，不是 MVP 验收。

## 0. 目标、判据与不变量

目标：**一张真实 BGW 需求从 iCafe 走到 RELEASE_SUCCESS**，再连续走通 2~4 张，覆盖单仓、双仓、RESTART、未知回执/环境失败四类场景。

验收按 [WORKFLOW_EXIT_CRITERIA.md](WORKFLOW_EXIT_CRITERIA.md) 的 C/M/P 三层分别给出，未测试的层写"未验证"。下表的数字是**实现预算和观察指标**，不是退出标准：

| 指标 | 现状 | MVP 目标 |
|---|---|---|
| 到达 RELEASE_SUCCESS 的真实 run | 0 / 11（BGW-1995 在途） | ≥1（两周内），≥3（一个月内）；人工在平台发布只记 `MANUAL_RELEASE_OBSERVED`，不计入 |
| 顺利路径上用户看到的审批卡 | G0~G9 共 9+ 次；BGW-1995 发布段另需 G9 + BranchPipeline 每个人工 stage 一张 G8 | 4 张（D、C、R、R'），由投影实现，内部门不删 |
| 控制面生产代码 | 约 40.3k 行（`tom-autodev/scripts` 非测试 `.py`） | 第 7 节删除阶段再定逐模块预算；不为减行删除保护 |

**必须保留的不变量**（MVP 不放松）：

1. 事件持久化 + 产物内容 hash；
2. 审批绑定准确的 `input_hash`，内容变了必须重批；
3. 真实副作用（iCode 提交、iPipe 触发、发布）先写 intent、后写 result，结果不明只查询、不盲目重发；
4. 冻结 `pipeline_plan`：发布只认本 run、准确 revision 的 build；
5. worktree 所有权：不覆盖用户改动；
6. 模型草案必须先过 schema 和前驱校验，才能锁 job；
7. 平台回执不能当身份证明：iPipe 人工 stage 执行只回 `{code, entities, msg}`。所有写操作统一为"写 → 有限次只读确认（stage 开始时间晚于 intent）→ 仍不明则只查询"；
8. 人工 stage 的审批绑定"前序 stage 已完成"：平台会把「发布」提前显示为 PENDING_FOR_USER，就绪前批的审批不能执行（`2913d3a`）；
9. 人工 stage 的参数只从流水线配置读（`stages[].paramBeans` / `inParamsMap`）；`manualParams=null` 不等于无参数，`required: false` 不等于可空。

**明确放弃的承诺**：

- 不保证旧 run 能跟随新 workflow 版本**写入**迁移；旧 run 保留只读投影；
- 不做跨 run 失败签名升级；
- 不保证"任何中途变更都能在原 run 里原地继续"。但在第 3.4 节 RESTART lineage 合同落地前，**重开不是默认路由**，只能在旧 run 无未知远程结果、经人工确认后进行。

## 1. 诊断：问题出在哪一层

- **架构方向正确**：事件持久化、内容 hash、intent/receipt、冻结 pipeline plan、worktree 所有权值得保留，不推倒重来，暂不迁移 Temporal。
- **设计层问题最突出**：状态和审批边界建模过细；v1 又试图一次性压扁（状态压缩、门合并、删旧能力、重做发布链放在同一批），验证面一下扩大。RESTART、C 门、R 门的授权与 lineage 合同没有定义。
- **实现层集中在外部平台契约**：BGW-1995 的 5 个发布修复全是平台语义问题——表达式跳过、双仓漏提、stage 回执无身份、stage 顺序、参数声明位置。
- **验证闭环是最大短板**：fake E2E 覆盖的是控制器假设，不是平台实际语义；手写 dict 的 fake API 与真实响应形状不一致，"fake 过、真实挂"反复出现。

因此顺序是：**投影 → 合同 → 真实场景矩阵 → 删除**。

## 2. 阶段与审批：先做用户可见投影，不做物理合并

内部保留现有细粒状态（INTAKE…RELEASE_SUCCESS、DIAGNOSE、ARCHITECTURE_REVIEW、ENVIRONMENT_BLOCKED、STOPPED）和 G0~G9。每个状态现在分别承担 schema、前驱 hash、审批、工作区所有权、task frontier 和外部副作用；直接压成 DESIGN/BUILD 会把复杂度搬进 bundle 内部，形成难以恢复和审计的隐式状态机（bundle schema、子 artifact provenance、task 级 checkpoint/attempt、半完成恢复都没有定义）。

新增一层 **milestone projection**：

| 用户看到 | 投影自（内部仍分别记录） | 卡片内容 | `approval_scope` |
|---|---|---|---|
| **D 设计** | G0 + G1 + G2 + G3 | 需求快照、profile/仓库/分支、决策日志、Spec、验收点、Task DAG | 内部各门的 `input_hash` 列表；任一内容变化则 D 失效 |
| **C 代码** | 每个 task 的 G4 + G5，及 G7 | 每个 task 的 plan 摘要、每个仓库的 diff、tom-review 结论、每个仓库的 iCode descriptor | 明确列出覆盖的 task 集合和仓库集合；只覆盖卡上列出的 task |
| **R 发布前** | G9 + BranchPipeline 发布前人工 stage 的 G8 | 见第 3.3 节 stage plan | 绑定 stage plan hash，见 3.3 |
| **R' 发布** | 「发布」stage 的 G8 | 前序全部完成的证据、将发布的 revision/branch | 单独一张卡，前序未完成不发出 |
| F 修复（仅失败） | G6 | 诊断 + 路由 | 不变 |
| P 重跑（仅失败） | G8 | 失败 stage、参数、build 绑定 | 不变 |

投影规则：

- 一张卡的同意，由控制器**展开**为内部各门的同意记录，每条仍绑定自己的 `input_hash`；台账、hash 绑定、送达回执、超时机制不变。
- 卡片在内部各门的输入全部就绪后才发出；没就绪的门不能被预批。
- 投影有自己的版本号；改投影规则不改 workflow spec hash。

**物理合并的触发条件**：投影在 3 张真实卡上稳定，且仍有明确收益（例如内部状态切换本身反复出错）时，再新增 `design-bundle` / `build-bundle` schema 和子状态协议。

**change class**：MVP 默认跑 `standard`。`full` 保留（它是复杂/跨仓需求的分段 Spec/Tasks 路径，不只是多几个门）；`express` / `hotfix` 暂保留，统计真实使用频率后再决定。

## 3. 四个合同（按已有真实证据排序落地）

### 3.1 IntentExecutor / Reconcile（证据：`cc83c3e`、`2913d3a`）

所有外部写（iCode push、iPipe trigger/rerun/manual execute、rebuild、发布）走同一个执行器：

```text
pre-check（只读，可失败，不写 intent） → claim intent → dispatch
  → 回执带身份：写 receipt
  → 回执无身份：有限次只读确认（身份 + 时间晚于 intent），确认则写 receipt
  → 仍不明：QUERY_REQUIRED，只允许 reconcile（GET），不重发
```

把 `ipipe_runtime` / `icode_runtime` 里分散的确认逻辑收进它；每种写操作只提供 `pre_check`、`dispatch`、`confirm(intent)` 三个函数。验收：WF-04、WF-05、WF-10 对应测试改为对执行器跑一遍。

### 3.2 多仓 C 提交集合（证据：`3485f49` 的漏提与 `submit-companion` 补救）

```text
C_PREPARE → SUBMIT_REPO[n]（每仓独立 intent/receipt） → SUBMIT_ALL_CONFIRMED（聚合结果） → TRIGGER_IPIPE[n]
```

- 任何一仓成功、另一仓未知或失败，都不能进入 IPIPE，也不能以"C 已批准"为继续依据；只能 reconcile 未知仓，或进 F。
- 聚合结果绑定 change set hash 和全部仓库的 revision；不再区分主仓与 companion。
- 修复路由区分 CR 状态：**CR 未合入**时同一 CR 出新 patchset，重新冻结 revision/build 绑定；**CR 已合入**时不能改该 CR，需新 CR（或新 run），并重新冻结。

### 3.3 R stage plan（证据：`3485f49`、`a0f0f92`、`2913d3a`）

RELEASE 的真实形态：CR 合入后 iCode 在目标分支启动 BranchPipeline（x86bgw 为 `348101`），串行经过编译 → 扫描 → 自动化回归测试（人工）→ 系统测试功能回归（人工）→ 提测（人工，通知 QA）→ 准出审核 → 测试准出（QA 在平台确认）→ 发布（人工）。`/api/agile/getReleaseInfo` 在网关上 404，发布身份只能从「发布」stage 的 `AGILE_RELEASE` job 读（`version` / `revision` / `branch` / `releaseOutputHttpUrl`）。

R 卡绑定一份 **stage plan**，hash 覆盖：

- stage 列表和顺序（stageConf id）；
- 流水线配置 hash（含各 stage 的 `paramBeans`）；
- 填参策略版本；
- 本 run 合入的 revision / branch；
- 每个 stage 的参数声明和脱敏值；
- 需要 QA 外部操作的 stage 及其完成证据的判定方式。

执行规则：

- 每个 stage 仍是独立 child intent/receipt（走 3.1）；
- 只执行第一个未完成且在等人工的 stage，执行前只读确认前序已完成（不变量 8）；
- 前一个 stage 结果未知时，不因 R 已批准就执行下一个；
- 配置、参数、revision 或前序状态与 plan 不一致时，R 失效，重新出卡；
- 「测试准出」由 QA 操作，控制器只等待并记录证据；
- 「发布」只能由 R' 授权；发布 job 成功且 revision/branch 与本 run 一致才写 RELEASE_SUCCESS。

### 3.4 RESTART lineage（证据：仅一次人工切换，第二张卡上验证）

`stop()` 现在只追加一条 STOPPED 事件，不处理旧 CR、build、pending intent；`adopt-code-only-repair` 只是一条很窄的 CODE_ONLY 承接路径，不等于通用重开。`restart-from` 落地前必须定义：

| 项 | 规则 |
|---|---|
| lineage | 新增 `source_run_id → target_run_id` 记录及 supersedes 关系；status 两端都显示 |
| 前提 | 旧 run 没有未 reconcile 的 intent；有则拒绝重开（`RESTART_SOURCE_UNRESOLVED`） |
| 旧远程对象 | 旧 CR：未合入的标记 abandon 候选并在卡上列出，由人确认；旧 build/BranchPipeline：加入排除集，不再作证据；旧 worktree：按所有权释放 |
| 可复制 | 已批准的需求快照、决策日志、Spec 等只读 artifact，作为新 run 的输入引用（保留原 hash 和来源） |
| 禁止复制 | 审批、intent、receipt、build 绑定 |
| 预算 | CODE_FIX 已用轮次继承；ENV_RETRY 不继承 |
| 需求快照 | 重新抓当前 iCafe 快照，记录与旧快照的差异，D 卡上展示 |

1fef8adb → 160c6840 的切换能顺利，是因为人工确认了旧 run 没有在途操作；这不是控制器保证。

## 4. 失败路由：closed-world

DIAGNOSE 的路由由 F 门确认，分类必须落在明确列表里：

| 路由 | 只适用于 | 去向 |
|---|---|---|
| `CODE_FIX` | CODE_FAILURE / TEST_FAILURE / 已确认的 REVIEW 阻断，且 Spec 不变 | 回 PLAN/IMPLEMENT；CR 是否已合入按 3.2 区分 |
| `ENV_RETRY` | ENV_* / PIPELINE_TRANSIENT / 产物过期 | P 门，重跑或 rebuild |
| `ENV_WAIT` | 环境未就绪、依赖不可用且有持久化证据 | 保留 ENVIRONMENT_BLOCKED：持久化等待原因、重试预算、复查条件 |
| `RESTART` | 仅 USER_REQUIREMENT_CHANGED / SPEC_CONFLICT / CONTRACT_INPUT_MISSING | 满足 3.4 前提后，经人工确认重开 |
| `STOP` | 未知分类、控制面 bug、权限问题、未 reconcile 的远程结果、证据不足 | STOPPED 或 F；只有完成 reconcile 或人工分类后才能改走其它路由 |

没有"其它 → RESTART"。预算由控制器按持久化事件强制（复用 `repair_policy.next_action`）：`CODE_FIX` 沿用现有规则（两轮无进展停、三次失败转架构复核、五轮上限）；`ENV_RETRY` 最多 3 次，超过转 `ENV_WAIT` 或 STOP。

## 5. 主路径证据合同

phase-protocol 现在规定：阶段完成必须有 KU receipt、iCafe 评论 receipt 和该阶段的审批。v1 想把 KU 降为 run 结束后的 best-effort，但只删调用不改合同会让流程卡住，直接放宽又会丢审计。决定如下，在动代码前先改 `phase-protocol.md`、completion predicate 和 `evidence_policy`：

| 证据 | 主路径必需 | 说明 |
|---|---|---|
| 本地 artifact（内容 hash + 前驱 hash） | 是 | 审计和恢复的唯一依据 |
| 审批记录（台账） | 是 | 不变 |
| iCafe 阶段评论 receipt | 否，降为旁路 | 失败记日志，run 结束时补发；状态迁移不等它 |
| KU 发布 receipt | 否，降为旁路 | run 结束后批量归档一次；审批卡链接改为本地快照 |

改合同时同步删除/改写依赖"KU receipt 是完成前提"的测试，保留不变量 1、2 的测试。

审批恢复不删：`reissue-approval`、`confirm-approval-redelivery`、`work-card reconcile` 收成一个受控的 `approval reconcile`（处理消息丢失、未知投递、重启恢复），低层命令隐藏。

## 6. 验证闭环：真实平台契约 fixture

v1/v2 漏掉的根因：fake API 用手写 dict，形状和语义由控制器作者假设。改为：

- 把 BGW-1995 拿到的真实 iPipe/iCode 响应录制成 fixture（token 脱敏、只保留结构和身份字段）：348101/348102 的 stage 列表与状态序列、`skipByStage: EXPRESSION`、人工执行回执 `{code, entities, msg}`、`paramBeans` / `inParamsMap`、`AGILE_RELEASE` job 详情、`getReleaseInfo` 404；
- fake API 回放 fixture；新增平台行为必须先录制再写代码；
- 每次控制面变更先写清楚影响哪些不变量和 WF 项，再跑对应的 fixture 场景。

## 7. 删除清单（第 4 步才执行，真实 3 张卡通过之后）

删除原则：按"MVP 路径在 3 张真实卡上没经过"来删，不按"代码写得不好"来删；删一个能力时同时删它的测试、文档段落和 CHANGELOG 承诺；旧 workflow 保留**只读 projector**，旧 run 仍能 `status` / `context`。

| 类别 | 候选 | 前提 |
|---|---|---|
| 原地恢复 / 迁移 | `recover-*`、`migrate-workflow-spec`、`repin-profile`、`revise`、`rewind`、`workflow_spec_migration.py`、`profile_repin.py`、`run_change.py` | `restart-from`（3.4）已在真实卡上验证 |
| 审批旁路 | `approve`、`request-approval`、`await-approval`、`correct-approval-message` | 投影 + `approval reconcile` 已覆盖其用途 |
| 手工推进 | `next`、`complete-phase`、`advance`、`drive`（并入 `continue`）、`start`（并入 `process`） | worker 在 3 张卡上无需它们 |
| 提交补救 | `submit-companion`、`orchestrator._submit_companion` | 3.2 多仓提交集合上线 |
| 发布旧路径 | `verify_release` 的 `release_info` 分支（网关 404）、legacy planless 分支 | 3.3 上线 |
| 无生产调用方 | `replay_gate.py`、`decision_questions.py`、`failure_signature_inventory.py` | 再确认一次无调用方 |
| 次要功能 | `optimize` / G10、`acceptance-candidates`、`notify-ide-turn`、`ide_turn_hook.py`、`backfill_knowledge.py` | 负责人确认不用 |

**保留**：`stage_parameters.py`、`product_policy.py`、`environment_evidence.py`（发布主路径要用）；`full` / `express` / `hotfix`（见第 2 节）；`ENVIRONMENT_BLOCKED`（见第 4 节）。

逐模块行数预算在本节执行前按实际调用图给出，统计口径为 `tom-autodev/scripts` 下非测试 `.py`。

## 8. 路线

顺序原则：**先拿到一次真实发布；每一步都有真实 run 作为验收；不把多个高风险变化放进同一批。**

### 第 0 步：用现有状态机把 BGW-1995 推到发布（已完成，2026-10-28）

- 冻结新功能，只修阻挡这个 run 的问题，每个修复一个 commit；
- 卡在控制面时允许人工在平台完成发布，但只记为 `MANUAL_RELEASE_OBSERVED` / `INCOMPLETE`，不计入 RELEASE_SUCCESS，也不计入 P 层资格。

**进展（2026-10-27）。** `1fef8adb` 承接 run 原地 rewind 失败（缺前驱，白批两次 CHANGE_REQUEST），人工确认无在途操作后停掉，`start --fresh` 为 `160c6840`，以旧 `67141c3` 作 IMPLEMENT 参考，一天内走到 BranchPipeline 发布段：CR 122967077（x86bgw）+ 122991865（测试库），ChangePipeline 275535376 SUCCESS，G9 已批，BranchPipeline 275700446 在跑「自动化回归测试」。途中修的发布路径问题（已提交，未 push）：

| commit | 问题 | 对方案的影响 |
|---|---|---|
| `3a50d6e` | 平台表达式跳过的 stage 被当成缺证据，build 永远 MONITORING | 3.3 通过判定；fixture（第 6 节） |
| `3485f49` | 测试库 commit 未提交；`getReleaseInfo` 404 | 3.2 多仓提交；3.3 发布身份 |
| `cc83c3e` | 人工 stage 执行回执无 stage id，每次都"结果不明" | 不变量 7；3.1 |
| `a0f0f92` + `2913d3a` | 回归未完时给「发布」发 G8 并被批准；修复后早批的 G8 不能执行 | 不变量 8；R / R' 拆分 |

另有一次纠正：「自动化回归测试」的 `get_bgwagent` 写在流水线配置 `paramBeans` 里，build 的 `manualParams` 为 null，曾被误判为无参数（不变量 9）。「P0新case回归」因测试环境未建好被平台表达式跳过，属临时豁免，环境建好后不得再跳过。

**结果（2026-10-28 11:03）：控制器到达 RELEASE_SUCCESS。** 这是 12 个真实 run 里第一次，由控制器执行，不是人工发布。

| 项 | 值 |
|---|---|
| 发布版本 | x86bgw `26.10.20.1`（release id 15480416） |
| revision / branch | `a9715d5` / `x86bgw-dpdk-2011-syncookie`；测试库 `8c68089`；bgwagent 未变，`0f0d516` |
| 产物 | `https://irepo.baidu-int.com/rest/prod/v3/baidu/sysip/x86bgw/releases/26.10.20.1/files`（下载需 IREPO token，未记录） |
| 证据 | ChangePipeline 275535376，BranchPipeline 275700446 |
| 耗时 | 10-26 新开 run 至 10-28 发布，约 2 天；BranchPipeline 约 21 小时，其中 100G p0 回归约 16 小时 |

审批次数（`approvals.sqlite`，本 run 共 16 张卡）：

| 门 | 同意 | 超时 | 备注 |
|---|---|---|---|
| G0 / G1 / G2 | 1 / 1 / 1 | — | 设计段 3 张 |
| G4 / G5 | 2 / 1 | — | |
| G7 | 2 | — | 主仓 + 测试库补提交 |
| ChangePipeline G8 | 2（含 1 张 `#retry-1`） | 1 | 348102 的人工 stage；一张超时后以 `#retry-1` 重发 |
| G9 | 1 | — | |
| BranchPipeline G8 | 4 | — | 自动化回归测试、提测、发布各 1 张有效；另 1 张是错发给「发布」并被批的 `c52c3b33`，已被 `2913d3a` 作废、未执行 |

有效审批 14 次（不计超时的 1 张和错发的 1 张）；发布段（G9 + BranchPipeline G8）占 4 次。按第 2 节的投影应是 D、C、R、R' 4 张卡。

人工介入（按第 8 节第 3 步的三类）：

| 类别 | 介入 |
|---|---|
| 控制面 bug | 5 次修复（见上表）；`RERUN_RESPONSE_INVALID` 手工 `--reconcile-only` 2 次；`submit-companion` 补提测试库 1 次；旧 watcher 误报 drift，杀进程 1 次 |
| 平台行为 | 「P0新case回归」被表达式跳过，用户接受（测试环境未建好的临时豁免）；QA 在 build 运行中从 348101 配置删除「系统测试功能回归」，控制器 fail-closed 停在 `STAGE_PARAMETERS_UNKNOWN`，由 QA 在平台跳过；「测试准出」由 yueyufei01 在平台确认 |
| 需求/基线 | 用户在 iCode 重建 syncookie 分支；case_http3 失败经 G6 诊断为基线缺 BGW-1918，rebase 后通过；旧承接 run `1fef8adb` 停掉重开 |

新发现，进入合同 3.3：BranchPipeline 的配置可以在 build 运行中被修改，所以 stage plan 要同时绑定 build 启动时的快照和实时配置；两者不一致时应停下来出卡，而不是报参数未知。

第 0 步已完成。后续：push 上述 commit 和 `pre-mvp` tag（待用户确认），再进入第 1 步。

### 第 1 步：fixture 与投影（约 1 周，可并行）

1. 第 6 节：录制 BGW-1995 的真实响应，fake API 改为回放；
2. 第 2 节：milestone projection + `approval_scope`，D/C 两张卡先上；内部门不动，workflow spec hash 不变；
3. watcher 启动时记录代码版本，与仓库不一致即退出，由 supervisor 拉起。

验收：现有全量测试 + 回放 fixture 的发布段场景通过；一张真实卡上用户只看到 D、C 两张前段卡。

### 第 2 步：合同 3.1 → 3.2 → 3.3（约 1~2 周，逐个合入）

每个合同单独一个批次：写合同 → 改实现 → fixture 场景 → 真实 run 验证 → 下一个。3.3 上线时 R / R' 卡一起上线。第 5 节的证据合同修改与 3.1 同批。

这些属于"状态机/授权/持久化/发布绑定的实质变更"，按退出标准第 5 节，每批对同一固定基线取两份独立评审，最多两轮修复→复核。

### 第 3 步：真实场景矩阵（约 2 周）

| 场景 | 要求 |
|---|---|
| 单仓 | 小而明确的 BGW bug 卡，到 RELEASE_SUCCESS |
| 双仓部分成功 | x86bgw + bgwagent（或测试库），至少观察到一次一仓成功、一仓未知或失败的处理 |
| RESTART | 合同 3.4 在此步落地并走一次；旧 run 远程对象处置有记录 |
| 未知回执 / 环境失败 | 至少一次 `QUERY_REQUIRED` → reconcile、一次 `ENV_RETRY` 或 `ENV_WAIT` |

全程记录每次人工介入的原因，分为"模型产出质量"、"平台行为"、"控制面 bug"三类，只修阻塞的控制面 bug。

验收：P 层——至少 3 张卡到 RELEASE_SUCCESS，场景矩阵每格有真实证据；C 层——WF-01~WF-10 证据齐全。

### 第 4 步：删除（约 1 周）

按第 7 节执行，每类一个 commit，每个 commit 全量测试通过；旧 workflow 保留只读 projector。

### 之后再决定

| 事项 | 触发条件 |
|---|---|
| 物理合并 DESIGN/BUILD | 投影在 3 张卡上稳定，且内部状态切换仍反复出错 |
| 迁移 Temporal | 调度、租约、轮询本身仍是主要故障来源（它解决不了 iPipe 身份、revision、stage 参数和发布证据语义） |
| 删除 `express` / `hotfix` | 真实使用频率证明不需要差异化 |
| 恢复 KU 阶段内发布 | 审批人明确需要在 KU 中读阶段文档 |

## 9. 风险与应对

| 风险 | 应对 |
|---|---|
| 投影卡太长，审批人看不完 | 卡片只展示摘要 + 验收点 + DAG / diff 统计；完整内容放本地快照链接 |
| 投影展开为内部同意时出错，等于一次批了没展示的内容 | 卡片只在内部各门输入全部就绪后发出；展开时逐门校验 `input_hash` 与卡上列出的一致 |
| R 卡一次授权多个 stage，审批人以为批的是"发布" | 卡片按顺序列每个 stage 做什么、是否可逆；「发布」永远是单独的 R' |
| 平台把下游人工 stage 提前显示为可操作，或回执不带身份 | 不变量 7、8；fixture 覆盖 |
| 后台 watcher 常驻旧代码，误报 `WORKFLOW_SPEC_DRIFT`（10-25 累计 8.4 万条，10-27 再次发生） | 第 1 步必做：watcher 感知代码版本 |
| 旧 run 在 RESTART 后仍自动合入或发布，新 run 重复提交 | 3.4：有未 reconcile intent 拒绝重开；旧 build 进排除集；旧 CR 人工确认处置 |
| 多仓一仓成功一仓未知，靠"C 已批准"继续 | 3.2：只认聚合结果 |
| 删掉的恢复入口在真实使用中又被需要 | 删除放到第 4 步；有 `pre-mvp` tag，按需挑回，需配真实 run 作证据 |
| 外部评审继续报出边界问题 | 按退出标准第 5 节有界评审；范围外的进改进清单 |
