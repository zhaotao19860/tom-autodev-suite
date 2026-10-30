# tom-autodev MVP 收敛方案

| 版本 | 日期 | 依据 |
|---|---|---|
| v1 | 2026-10-25 | 基线 `64d41e7`；11 个真实 run 无一到达发布 |
| v2 | 2026-10-27 | BGW-1995（run `160c6840`）第一次真实走进发布段，发布路径修了 5 个控制面问题 |
| v3 | 2026-10-27 | Codex 静态评审（基线 `2913d3a`）：改为"先投影、补合同、真实验证、最后删除" |
| v4 | 2026-10-28 | Codex 复审（基线 `5c162d4`）：投影改为 prepare + scope 授权；合同写到可实现：intent 状态机与逐操作确认、不可变 attempt、stage plan 快照与逐 stage 消费、lineage 表与原子性；LegacyRunProjector 先于删除；场景矩阵逐格定义完成条件。v4 是最后一版纯文档修订，之后边实现边改 |

v4 在 `mvp` 分支上，基于 `5c162d4`。本文件是计划，不是 MVP 验收。

## 0. 目标、判据与不变量

目标：**一张真实 BGW 需求从 iCafe 走到 RELEASE_SUCCESS**，再连续走通 2~4 个独立 run，覆盖单仓、双仓、RESTART、未知回执/环境失败四类场景。2026-10-30 起，这些 run 允许复用同一张测试卡 BGW-2019（见第 8 节第 3 步的口径说明）。

验收按 [WORKFLOW_EXIT_CRITERIA.md](WORKFLOW_EXIT_CRITERIA.md) 的 C/M/P 三层分别给出，未测试的层写"未验证"。下表的数字是**实现预算和观察指标**，不是退出标准：

| 指标 | 现状 | MVP 目标 |
|---|---|---|
| 到达 RELEASE_SUCCESS 的真实 run | 0 / 11（BGW-1995 在途） | ≥1（两周内），≥3（一个月内）；人工在平台发布只记 `MANUAL_RELEASE_OBSERVED`，不计入 |
| 顺利路径上用户看到的审批卡 | G0~G9 共 9+ 次；BGW-1995 发布段另需 G9 + BranchPipeline 每个人工 stage 一张 G8 | 4 张（D、C、R、R'），由 prepare + scope 授权实现，内部门号与台账不删 |
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

因此顺序是：**fixture 与版本感知 → 合同 → scope 授权 → 真实场景矩阵 → 只读投影 → 删除**。

## 2. 阶段与审批：改门的语义，不做物理合并

内部保留现有细粒状态（INTAKE…RELEASE_SUCCESS、DIAGNOSE、ARCHITECTURE_REVIEW、ENVIRONMENT_BLOCKED、STOPPED）和 G0~G9 的门号与台账。直接压成 DESIGN/BUILD 会把复杂度搬进 bundle 内部（bundle schema、子 artifact provenance、task 级 checkpoint、半完成恢复都没有定义），不做。

**v3 的"投影"有时序矛盾，v4 改为"prepare → 单次授权"。** G0→G1→G2→G3 和 G4→G5→G7 是串行的：后一个门的内容要等前一个门批完才生成。一张卡如果在开头发出，就是在批尚未生成的内容；如果在末尾发出，只是汇总，审批次数并没有减少。能真正减少卡数的只有一种做法：**中间门不再由人逐个批**，产物连续生成、逐个过机械校验，最后一张卡绑定全部产物的 hash。这是改门的语义（扩展现有只覆盖 SPEC+TASKS 的 `merged` 模式），要如实标注，不能叫"投影"。

| 用户卡 | prepare 段（无人审批，逐个机械校验） | 卡片绑定 | 批准后授权 |
|---|---|---|---|
| **D 设计** | 决策日志 → Spec → DAG；每份过 schema + 前驱 hash；GRILL 有待用户回答的问题时**必须停下问**，答完才继续 | `scope_hash` = 需求快照、profile、三份 artifact 的 hash | 进入 WORKSPACE |
| **C 代码** | 对**卡上列出的 task 集合**：plan → change set → tom-review；review 非 PASS 不出卡，转 DIAGNOSE | `scope_hash` = 每个 task 的 plan/change set/review hash + 每个仓库的 iCode descriptor | 提交卡上列出的仓库（3.2），按冻结 plan 触发 iPipe |
| **R 发布前** | 读 BranchPipeline 配置，生成 stage plan（3.3） | stage plan hash | 按序执行 plan 内的可逆人工 stage，逐个消费（3.3） |
| **R' 发布** | 前序全部完成 | 前序证据 + 发布 revision/branch | 执行「发布」stage |
| F 修复 / P 重跑 | 不变 | 不变 | 不变 |

授权规则：

- 单次授权的记录叫 **scope approval**，字段：`scope_schema`、`scope_version`、`scope_policy_hash`、`scope_hash`、展开的内部门列表（每项 `gate` + `input_hash`）、审批人。批准时控制器为每个内部门写一条派生同意，`effective_channel = scope:<approval_id>`，可在台账追溯。
- **审批人权限取交集**：scope 卡的有效审批人必须同时在所含每个内部门的成员策略里；交集为空时不出 scope 卡，退回逐门审批。
- **只能在 prepare 结束后出卡**，卡上所有 hash 都已存在；之后任何一份 artifact 变化，`scope_hash` 不再匹配，派生同意全部失效，需重出卡（不变量 2）。
- **防重放**：派生同意只对 `scope_hash` 内列出的 `input_hash` 有效；同一 `scope_hash` 重复批准幂等；scope 规则改版（`scope_policy_hash` 变）后，旧 scope 批准不能用于新 prepare。
- C 卡只覆盖卡上列出的 task；DAG 有多个 task 时，每批 frontier 一张 C 卡，不预批后续 task。
- 失败路径上 prepare 中断（review 不过、校验失败）时，已生成的 artifact 保留，不出卡，按第 4 节路由。

**卡数预期**：单 task 顺利路径 D、C、R、R' 共 4 张；BGW-1995 实际 14 次有效审批。

**物理合并的触发条件**：scope 授权在 3 个真实独立 run 上稳定，且仍有明确收益（例如内部状态切换本身反复出错）时，再新增 `design-bundle` / `build-bundle` schema 和子状态协议。

**change class**：MVP 默认跑 `standard`。`full` 保留（它是复杂/跨仓需求的分段 Spec/Tasks 路径，不只是多几个门）；`express` / `hotfix` 暂保留，统计真实使用频率后再决定。

## 3. 四个合同（按已有真实证据排序落地）

### 3.1 IntentExecutor / Reconcile（证据：`cc83c3e`、`2913d3a`）

**intent 状态机**（持久化在 `external_intents` + `external_results`，状态只能前进）：

```text
CLAIMED ──dispatch 返回带身份──────────────▶ CONFIRMED（终态，写 receipt）
   │    └─dispatch 前本地错误（已证明未发送）▶ ABANDONED_LOCAL（终态，可新 intent 重试）
   │
   └─回执无身份/超时/异常─▶ UNKNOWN ─只读确认命中唯一候选─▶ CONFIRMED
                              ├─只读确认证明未发生─────────▶ CONFIRMED_ABSENT（终态，可新 intent 重试）
                              ├─命中多个候选────────────────▶ AMBIGUOUS（终态，禁止重发，转人工）
                              └─仍不能判断──────────────────▶ 保持 UNKNOWN（只允许 reconcile）
```

- **重发规则**：只有 `ABANDONED_LOCAL` 和 `CONFIRMED_ABSENT` 允许同一操作再开新 intent；`UNKNOWN` / `AMBIGUOUS` 存在时，同一目标对象的所有写都被阻断（现有 `claim_exclusive_intent` 的前缀互斥即这一 CAS，保留）。
- **租约**：claim 在 run 租约内完成；租约过期的 worker 不能写 receipt，只有持有当前租约者能推进 intent 状态。
- **observation**：每次只读确认写一条 observation（`intent_id`、查询的对象、看到的候选 id 列表、判定结果、平台时间戳与本地时间戳），作为 `CONFIRMED_ABSENT` / `AMBIGUOUS` 的证据。

**每种操作的确认合同**（不共用一个泛化 `confirm`）：

| 操作 | 唯一身份 | 无身份回执时的确认 | 判 ABSENT 的条件 | 多候选 |
|---|---|---|---|---|
| iCode push（新 CR） | Change-Id → CR number | 按 Change-Id 查 CR | 查询成功且无该 Change-Id 的 CR | 同 Change-Id 多 CR → AMBIGUOUS |
| iCode push（追加 patchset） | CR number + patchset 的 commit sha | 查 CR 当前 patchset 列表 | 列表成功且无该 sha | 不适用 |
| iPipe trigger（新 build） | `builds_by_revision(module, revision, pipeline)` 中 `createTime` 晚于 intent 的 build | 同左 | 窗口（intent 后 10 分钟）内列表成功且无新 build | 晚于 intent 的新 build 多于 1 个 → AMBIGUOUS |
| iPipe 人工 stage 执行 / 失败重跑 | stage_build_id（平台复用同一 id） | stage `startTime` 晚于 intent − 30 s 偏差，且状态离开 PENDING/WAITTING | 不判 ABSENT：stage 仍 PENDING 只保持 UNKNOWN，人工确认后才可 `CONFIRMED_ABSENT` | 不适用 |
| BranchPipeline 发布 | AGILE_RELEASE job 的 release id + revision + branch | 读「发布」stage 的 job 详情 | 同上，不自动判 ABSENT | 同 revision 多个 release → AMBIGUOUS |
| iPipe rebuild | 同 trigger | 同 trigger | 同 trigger | 同 trigger |

时钟：只用平台时间戳与 intent 的平台对齐时间比较；本地时间只记录不判定。偏差允许值随操作写在表里，不全局配置。

实现：`IntentExecutor.run(op, target, approval)`，每种操作注册 `pre_check`、`dispatch`、`identity(response)`、`observe(intent)` 四个函数。验收：WF-04、WF-05、WF-10 对应测试改为对执行器跑一遍，每种操作的 `UNKNOWN → CONFIRMED / CONFIRMED_ABSENT / AMBIGUOUS` 各有用例。

### 3.2 多仓 C 提交：不可变 attempt（证据：`3485f49` 的漏提与 `submit-companion` 补救）

三类记录都**只追加、不修改**：

| 记录 | 内容 | 由什么产生 |
|---|---|---|
| `change_set_attempt` | attempt 序号、全部仓库的 change set hash、对应 C 卡 `scope_hash` | 每次 BUILD 产出一份新 change set |
| `revision_set` | 每个仓库的 CR number + 实际提交 sha | 该 attempt 的全部仓库都 CONFIRMED |
| `pipeline_plan_attempt` | 冻结的模块 / pipeline / revision 绑定 | 由 `revision_set` 生成 |

提交流程：

```text
C 批准(scope_hash) → SUBMIT_REPO[i]（每仓一个 3.1 intent）
  → 全部 CONFIRMED：写 revision_set，聚合状态 SUBMITTED，进 IPIPE
  → 任一 UNKNOWN：只 reconcile 该仓；其它仓不重发
  → 任一 AMBIGUOUS 或失败（非 ABSENT）：聚合状态 PARTIAL，转 F
```

部分成功后的规则：

- **成功仓的 CR 复用**：重试只针对失败仓，成功仓的 CONFIRMED receipt 保留。
- **失败仓重试是否要新 C**：原因是平台失败且 change set 不变（如 push 被拒后权限修好）→ 走 P 门重试，同一 attempt；需要改代码 → 新 `change_set_attempt`，重新过 C 卡。
- **pre-check 到提交之间 CR 状态变化**（被人 abandon / 合入 / 他人追加 patchset）：pre-check 读到的 CR 状态和 patchset 写进 intent；dispatch 后确认时不一致 → AMBIGUOUS，转 F。
- **进入 IPIPE 的唯一依据**是聚合状态 SUBMITTED，不是"C 已批准"。聚合状态的写入用 CAS（`expected_attempt` + `expected_status`），并发写只有一个成功。

修复路由按 CR 是否合入：

- **CR 未合入**：新 `change_set_attempt` → 同一 CR 上新 patchset → 新 `revision_set` → 新 `pipeline_plan_attempt`；旧 attempt 的 build 标 `SUPERSEDED`，不再作证据（现有 `ipipe.build-rejected` / 排除集机制承担）。
- **CR 已合入**：不能改该 CR；新 attempt 走新 CR，或按 3.4 RESTART。
- **发布只选最新的有效 attempt**；有更新的 attempt 处于 PARTIAL 时，旧 attempt 也不能发布。

### 3.3 R stage plan：快照 + 逐 stage 消费（证据：`3485f49`、`a0f0f92`、`2913d3a`，以及 10-28 配置在运行中被改）

RELEASE 的真实形态：CR 合入后 iCode 在目标分支启动 BranchPipeline（x86bgw 为 `348101`），串行经过编译 → 扫描 → 自动化回归测试（人工）→ 系统测试功能回归（人工）→ 提测（人工，通知 QA）→ 准出审核 → 测试准出（QA 在平台确认）→ 发布（人工）。`/api/agile/getReleaseInfo` 在网关上 404，发布身份只能从「发布」stage 的 `AGILE_RELEASE` job 读。

**stage plan schema**（`stage-plan.v1`）：

```text
branch_build_id, pipeline_conf_id, revision, branch
build_snapshot:  build 自身的 stage 列表（stageBuildId, stageConfId, stageName, 顺序）
config_snapshot: get_pipeline_by_id(brief=false) 的 stages[] 规范化后取
                 {stageConfId, stageName, upstreamStageId, paramBeans[name,required], inParamsMap keys,
                  groupJobs[jobConfId, jobType]}，按 stageConfId 排序，键名排序后 JSON，sha256
config_hash, fill_policy_version
stages[]: {stage_build_id, stage_conf_id, kind: MANUAL|QA|RELEASE|AUTO,
           parameters: {name: 脱敏值}, nonce}
```

10-28 实测：QA 在 build 运行中从配置删掉了「系统测试功能回归」，build 仍按启动时的快照带着它。所以 **build 快照决定"有哪些 stage、什么顺序"，配置快照只用来读参数声明**；某 stage 在配置里找不到时，以 build 自身声明为准（`manualParams` + `hasDynamicParams=false` 为显式无参），并在卡上标注"配置已删除此 stage"，而不是报 `STAGE_PARAMETERS_UNKNOWN` 停下。

**消费规则**：

- R 批准后，每个 MANUAL stage 用自己的 `nonce` 消费一次：执行前重新只读拉取 build 快照和配置快照，与 plan 一致才 claim intent（3.1）；消费后 nonce 标 consumed，不能再用。
- **GET 到 POST 之间配置变化**：claim 前重读一次配置，hash 变了不 claim；POST 之后才发现变化的，该 stage 的结果照常按 3.1 确认，后续 stage 停下。
- **任一 stage 失败、UNKNOWN、AMBIGUOUS，或前序证据 / 配置 / revision 漂移**：R 对剩余 stage 失效。重试走 P 门（同 stage），继续走新 R 卡（剩余 stage 的新 plan）。不存在一张 R 批准自动越过异常。
- **QA stage**（测试准出等）：控制器不执行，只等待。plan 里记录 `expected_actor`（来自 profile 的 QA 成员）和 `max_wait`（默认 72 小时）。完成证据为 stage 状态 SUCC 且平台记录的操作人在 QA 成员内；超时 → `ENV_WAIT`（第 4 节）提醒，不自动失效 R；QA 在平台驳回 → stage 失败，按上一条处理。
- **RELEASE stage** 只能由 R' 消费。

### 3.4 RESTART lineage（证据：仅一次人工切换，第二张卡上验证）

`stop()` 现在只追加一条 STOPPED 事件，不处理旧 CR、build、pending intent；`adopt-code-only-repair` 只是很窄的 CODE_ONLY 承接，不等于通用重开。

**存储**：新表 `run_lineage(source_run_id PRIMARY KEY, target_run_id UNIQUE, idempotency_key UNIQUE, created_at, payload_json)`。一个 source 只能被重开一次；同一 `idempotency_key`（`restart:<source>:<D 卡 scope_hash 前缀>`）重复调用返回已有 target。

**原子性**：在一个 SQLite 事务里完成：CAS 检查 source 状态（`expected_state` ∈ 非终态或 STOPPED）→ source 写 STOPPED（若未停）→ 插 lineage → 建 target run 的 INTAKE 事件。任一步失败全部回滚。

**前提检查**（任一不满足即 `RESTART_SOURCE_UNRESOLVED`，不建 target）：

- source 没有 `UNKNOWN` / `AMBIGUOUS` intent；
- source 的每个远程对象都有处置结论（见下表），没有 `ABANDONED_UNKNOWN`；
- source 自有 worktree 无未提交改动；有则阻断，由人决定提交或丢弃（不变量 5）。

**旧远程对象处置**：

| 对象 | 可能状态 | 处置 |
|---|---|---|
| 未合入的 CR | `ABANDONED_CONFIRMED`（在 iCode 上 abandon 并查询确认）/ `KEPT_FOR_REFERENCE`（人确认不合入，但保留） / `ABANDONED_UNKNOWN`（发了 abandon、未确认） | 前两者允许重开；`ABANDONED_UNKNOWN` 阻断 |
| 已合入的 CR | 合入即会触发 BranchPipeline，本地排除集拦不住 | 默认**不允许** RESTART；需人确认该次合入的处置（回滚 CR、或接受其发布），记录后才允许 |
| ChangePipeline / BranchPipeline build | 运行中 / 结束 | 运行中的 build 需在平台取消并查询确认，或等其结束；之后加入排除集 |
| worktree | 干净 / 脏 | 干净的按所有权释放；脏的阻断 |

**artifact 复制**：旧 run 的需求快照、决策日志、Spec、DAG 只作为 target **DESIGN 的参考输入**引用（保留原 hash 和来源），**不能直接作为 WORKSPACE/PLAN 的前驱**；target 必须重新产出 D 段 artifact 并过 D 卡。禁止复制审批、intent、receipt、build 绑定。

**需求快照冲突**：target 重新抓 iCafe 快照，与 source 快照做差异；差异涉及验收点或范围时，生成 `requirement-diff` artifact 并在 D 卡上置顶展示，prepare 不能跳过 GRILL。

**预算**：CODE_FIX 已用轮次继承（防止靠重开刷预算）；ENV_RETRY 不继承。

1fef8adb → 160c6840 的切换能顺利，是因为人工确认了旧 run 没有在途操作，且旧 CR 未合入；这不是控制器保证。

## 4. 失败路由：closed-world

DIAGNOSE 的路由由 F 门确认，分类必须落在明确列表里：

| 路由 | 只适用于 | 去向 |
|---|---|---|
| `CODE_FIX` | CODE_FAILURE / TEST_FAILURE / 已确认的 REVIEW 阻断，且 Spec 不变 | 回 PLAN/IMPLEMENT；CR 是否已合入按 3.2 区分 |
| `ENV_RETRY` | ENV_* / PIPELINE_TRANSIENT / 产物过期 | P 门，重跑或 rebuild |
| `ENV_WAIT` | 环境未就绪、依赖不可用、QA stage 超时，且有持久化证据 | 保留 ENVIRONMENT_BLOCKED；持久化 `wait_reason`、`next_actor`（谁来解除）、`recheck_after`、`max_wait`（超过转 STOP）、`evidence_refresh`（复查时重新取哪份证据）；只有新证据显示解除才离开，不接受口头解除 |
| `RESTART` | 仅 USER_REQUIREMENT_CHANGED / SPEC_CONFLICT / CONTRACT_INPUT_MISSING | 满足 3.4 前提后，经人工确认重开 |
| `STOP` | 未知分类、控制面 bug、权限问题、未 reconcile 的远程结果、证据不足 | STOPPED 或 F；只有完成 reconcile 或人工分类后才能改走其它路由 |

没有"其它 → RESTART"。预算由控制器按持久化事件强制（复用 `repair_policy.next_action`）：`CODE_FIX` 沿用现有规则（两轮无进展停、三次失败转架构复核、五轮上限）；`ENV_RETRY` 最多 3 次，超过转 `ENV_WAIT` 或 STOP。

## 5. 主路径证据合同

phase-protocol 现在规定：阶段完成必须有 KU receipt、iCafe 评论 receipt 和该阶段的审批。v1 想把 KU 降为 run 结束后的 best-effort，但只删调用不改合同会让流程卡住，直接放宽又会丢审计。决定如下，在动代码前先改 `phase-protocol.md`、completion predicate 和 `evidence_policy`。新合同带版本号 `evidence-contract.v2`，**按 run 创建时绑定的版本读**：旧 run 继续按 v1 判定完成，新 run 按 v2；双读期间两套 predicate 并存，旧 run 全部到终态后才删 v1。

| 证据 | 主路径必需 | 说明 |
|---|---|---|
| 本地 artifact（内容 hash + 前驱 hash） | 是 | 审计和恢复的唯一依据 |
| 审批记录（台账） | 是 | 不变 |
| iCafe 阶段评论 receipt | 否，降为旁路 | 失败记日志，run 结束时补发；状态迁移不等它 |
| KU 发布 receipt | 否，降为旁路 | run 结束后批量归档一次；审批卡链接改为本地快照 |

改合同时同步删除/改写依赖"KU receipt 是完成前提"的测试，保留不变量 1、2 的测试。

审批恢复不删：`reissue-approval`、`confirm-approval-redelivery`、`work-card reconcile` 收成一个受控的 `approval reconcile`（处理消息丢失、未知投递、重启恢复），低层命令隐藏。

## 6. 验证闭环：真实平台契约 fixture

v1/v2 漏掉的根因：fake API 用手写 dict，形状和语义由控制器作者假设。改为回放 fixture。

**fixture schema**（`platform-fixture.v1`，一份一个文件，放 `tom-autodev/scripts/tests/fixtures/platform/`）：

```text
fixture_id, origin: RECORDED | DERIVED, source_run_id, source_object (build/stage/CR id),
platform (ipipe|icode), endpoint, captured_at, redaction: [字段路径], content_sha256,
derived_from (DERIVED 时必填：源 fixture_id + 改了什么), response
```

`DERIVED` 不能冒充 `RECORDED`；测试报告按 origin 分开计数。

| 场景 | 来源 |
|---|---|
| 348101/348102 的 stage 列表与状态序列、`skipByStage: EXPRESSION` | RECORDED（BGW-1995） |
| 人工执行回执 `{code, entities, msg}`（无身份） | RECORDED |
| `paramBeans` / `inParamsMap`、`manualParams=null` + `hasDynamicParams=false` | RECORDED |
| 「发布」提前显示 PENDING_FOR_USER、前序未完成 | RECORDED |
| build 运行中配置删除 stage（5993070） | RECORDED |
| `AGILE_RELEASE` job 详情、`getReleaseInfo` 404 | RECORDED |
| 产物下载 403/404 | RECORDED（BGW-1995 早期 rebuild 前） |
| 5xx、权限拒绝、同 revision 多候选 build、stage id 复用、平台时钟偏差 | DERIVED（按真实响应形状构造），遇到真实响应后替换为 RECORDED |

规则：新增平台行为必须先录 fixture 再写代码；每次控制面变更先写清楚影响哪些不变量和 WF 项，再跑对应的 fixture 场景。

## 7. 删除清单（第 4 步才执行）

**不能按文件名删。** 10-28 核对调用方：`workflow_spec_migration` 被 6 个生产文件引用（`orchestrator`、`agent_bridge`、`approval_summary`、`run_brief`、`run_change`、`workflow_spec`），`profile_repin` 被 5 个（含 iCode/iPipe runtime），`run_change` 被 6 个（含 `approval_watch`、`approval_delivery`）。

**第一步是 `LegacyRunProjector`**：只提供 `status(run_id)` 和 `context(run_id)`，只读 events / artifacts / approval ledger，不依赖 live workflow spec、`next()`、migration、repin 或任何外部查询。它在删除任何模块之前上线，并对库里全部历史 run 跑一遍、与现有 `status` 输出对照。

**删除的前提**（全部满足才动一个类别）：

1. 第 8 节第 3 步场景矩阵每格都有真实证据；
2. 库里没有非终态的旧 workflow run；
3. 没有 `UNKNOWN` / `AMBIGUOUS` / `QUERY_REQUIRED` intent；
4. 旧 run 的远程对象（CR / build / BranchPipeline）都已处置；
5. `LegacyRunProjector` 对全部历史 run 的 status/context 演练通过；
6. import 图、CLI 注册、docs/skill 文本扫描证明该类别无生产调用方。

| 类别 | 候选 | 额外前提 |
|---|---|---|
| 原地恢复 / 迁移 | `recover-*`、`migrate-workflow-spec`、`repin-profile`、`revise`、`rewind`、`workflow_spec_migration.py`、`profile_repin.py`、`run_change.py` | `restart-from`（3.4）已在真实卡上验证 |
| 审批旁路 | `approve`、`request-approval`、`await-approval`、`correct-approval-message` | scope 授权 + `approval reconcile` 已覆盖 |
| 手工推进 | `next`、`complete-phase`、`advance`、`drive`（并入 `continue`）、`start`（并入 `process`） | worker 在真实卡上无需它们 |
| 提交补救 | `submit-companion`、`orchestrator._submit_companion` | 3.2 上线 |
| 发布旧路径 | `verify_release` 的 `release_info` 分支（网关 404）、legacy planless 分支 | 3.3 上线 |
| 无生产调用方 | `replay_gate.py`、`decision_questions.py`、`failure_signature_inventory.py` | 前提 6 |
| 次要功能 | `optimize` / G10、`acceptance-candidates`、`notify-ide-turn`、`ide_turn_hook.py`、`backfill_knowledge.py` | 负责人确认不用；`acceptance-candidates`、`notify-ide-turn` 现仍有调用，先迁走调用方 |

**保留**：`stage_parameters.py`、`product_policy.py`、`environment_evidence.py`；`full` / `express` / `hotfix`；`ENVIRONMENT_BLOCKED`。逐模块行数预算在本节执行前按调用图给出。

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

有效审批 14 次（不计超时的 1 张和错发的 1 张）；发布段（G9 + BranchPipeline G8）占 4 次。按第 2 节的 scope 授权应是 D、C、R、R' 4 张卡。

人工介入（按第 8 节第 3 步的三类）：

| 类别 | 介入 |
|---|---|
| 控制面 bug | 5 次修复（见上表）；`RERUN_RESPONSE_INVALID` 手工 `--reconcile-only` 2 次；`submit-companion` 补提测试库 1 次；旧 watcher 误报 drift，杀进程 1 次 |
| 平台行为 | 「P0新case回归」被表达式跳过，用户接受（测试环境未建好的临时豁免）；QA 在 build 运行中从 348101 配置删除「系统测试功能回归」，控制器 fail-closed 停在 `STAGE_PARAMETERS_UNKNOWN`，由 QA 在平台跳过；「测试准出」由 yueyufei01 在平台确认 |
| 需求/基线 | 用户在 iCode 重建 syncookie 分支；case_http3 失败经 G6 诊断为基线缺 BGW-1918，rebase 后通过；旧承接 run `1fef8adb` 停掉重开 |

新发现，进入合同 3.3：BranchPipeline 的配置可以在 build 运行中被修改，所以 stage plan 要同时绑定 build 启动时的快照和实时配置；两者不一致时应停下来出卡，而不是报参数未知。

第 0 步已完成。后续：push 上述 commit 和 `pre-mvp` tag（待用户确认），再进入第 1 步。

### 第 1 步：watcher 版本感知 + fixture（已完成，2026-10-28）

1. watcher 启动时记录代码版本（git HEAD + 控制面文件 hash），每轮比对，不一致即退出，由 supervisor 拉起新代码；
2. 第 6 节：录制 BGW-1995 的 RECORDED fixture，fake API 增加回放模式；现有发布段测试至少各有一条改为回放；
3. 不改状态机、不改审批语义、workflow spec hash 不变。

验收：全量测试通过；回放 fixture 的发布段场景（表达式跳过、无身份回执、发布提前 PENDING、配置删除 stage）通过。

**结果：** `e52fc0a` watcher 版本感知；fixture 12 份（全部 RECORDED），回放测试 9 项，全量 1496 通过。无身份回执和「发布」提前 PENDING 的原始响应已过时无法补录：前者只留有 observation（`response_keys: [code, entities, msg]`），后者 build 已结束；两者在第 2 步合同 3.1 / 3.3 实现时按 observation 派生为 DERIVED fixture，并在下一个真实 run 中补录。

### 第 2 步：合同逐个合入（约 2~3 周）

**进展：** 3.1 的状态机已落地；已迁入全部 6 种写操作（iPipe 人工 stage 执行、trigger、rebuild、iCode 新 CR / 追加 patchset、BranchPipeline 发布身份）。**3.1 完成。** **第 5 节证据合同 v2 完成**（INTAKE 固定合同版本，旧 run 按 v1 双读）。 **3.2 聚合状态完成**（`submission_set.py`）。 **3.3 stage plan 绑定完成**（`stage_plan.py`）。 **第 2 节 scope 授权完成台账层**（`request_scope` / `expand_scope`）。

**尚未完成、需要真实 run 或人决定的部分：**

1. scope 授权接入 worker（D 卡 = GRILL→SPEC→TASKS 的 prepare 段，C 卡 = PLAN→IMPLEMENT→REVIEW 的 prepare 段，R 卡 = BranchPipeline 发布前 stage 链）。这一步改变中间门"必须有人批"的语义，按本方案的顺序原则，要在 3.1~3.3 的实现先经一张真实卡验证后再做；
2. 合同 3.4 RESTART lineage：**实现完成（2026-10-30，`restart.py` + `run_lineage` 表 + `restart-from` 命令）**，待 S5 真实 run 验证；
3. 第 3 步真实场景矩阵 S1~S7、第 4 步 LegacyRunProjector 与删除，都依赖真实 run。

顺序：**3.1 → 第 5 节证据合同 v2（双读）→ 3.2 → 3.3 → 第 2 节 scope 授权**。每个合同单独一个批次：实现 → fixture 场景（含 DERIVED 负例）→ 真实 run 验证 → 下一个。scope 授权放最后，因为它依赖 3.2/3.3 的 attempt 和 plan 结构。

这些属于"状态机/授权/持久化/发布绑定的实质变更"，按退出标准第 5 节，每批对同一固定基线取两份独立评审，最多两轮修复→复核。

### 第 3 步：真实场景矩阵（约 2~3 周）

每格单独定义完成条件，不都套 RELEASE_SUCCESS：

| 场景 | 覆盖 P 层要求 | 完成条件 |
|---|---|---|
| S1 单仓正常交付 | 正常交付、人工阶段 | RELEASE_SUCCESS，审批 4 张卡 |
| S2 双仓正常交付 | 多模块、人工阶段 | 两仓 CR 均 CONFIRMED，`revision_set` 含两仓，RELEASE_SUCCESS |
| S3 失败→诊断→修复→重新提交→发布 | 失败修复闭环 | 至少一次 CODE_FIX 新 attempt、旧 build SUPERSEDED，最终 RELEASE_SUCCESS |
| S4 双仓部分成功 | 部分成功处理 | 观察到 PARTIAL 或单仓 UNKNOWN → reconcile；**没有**错误进入 IPIPE；结局可以是修好后发布，也可以是 STOP，以记录完整为准 |
| S5 RESTART | lineage | `run_lineage` 一行；旧远程对象处置表完整；target 重新过 D 卡 |
| S6 审批后续跑 / 中断恢复 | 中断恢复 | 在 claim 后、receipt 前杀进程，重启后 reconcile 到正确终态，无重复写 |
| S7 未知回执 / 环境等待 | 未知结果、环境失败 | 至少一次 `UNKNOWN → CONFIRMED`、一次 `ENV_WAIT` 按 `recheck_after` 复查后离开或转 STOP |

S1~S3 可以由同 1~3 个 run 覆盖；S4、S6、S7 允许在这些 run 里自然出现或受控制造（例如审批后人为中断 worker），但必须是真实平台上的真实 run。

**口径（2026-10-30 调整）**：计数单位是独立 run，不是卡。各 run 可复用测试卡 BGW-2019 及其专用分支（x86bgw / bgwagent `x86bgw-dpdk-2011-autodev`、qa `pipline_case_autodev`，不上线），上一个 run 终态后用 `start --fresh` 开新 run，旧 run 记录全部保留；每个 run 须有自己的代码改动、CR 与 build。已知代价：GRILL/SPEC 的输入相近，覆盖不到不同类型的需求，这一项在 P 层记为"未验证"。受控制造故障（S4、S6 的中断、S5 的需求变更）前，须先向需求方说明中断点与会留下的远程对象，确认后执行。全程记录人工介入原因（模型产出质量 / 平台行为 / 控制面 bug），只修阻塞的控制面 bug。

验收：P 层——S1~S7 每格有真实证据，至少 3 个独立 run 到 RELEASE_SUCCESS（可同卡）；C 层——WF-01~WF-10 证据齐全。

### 第 4 步：LegacyRunProjector 与删除（约 1~2 周）

先上 `LegacyRunProjector` 并对全部历史 run 演练；再按第 7 节的前提逐类删除，每类一个 commit，每个 commit 全量测试通过。

### 之后再决定

| 事项 | 触发条件 |
|---|---|
| 物理合并 DESIGN/BUILD | scope 授权在 3 个独立 run 上稳定，且内部状态切换仍反复出错 |
| 迁移 Temporal | 调度、租约、轮询本身仍是主要故障来源（它解决不了 iPipe 身份、revision、stage 参数和发布证据语义） |
| 删除 `express` / `hotfix` | 真实使用频率证明不需要差异化 |
| 恢复 KU 阶段内发布 | 审批人明确需要在 KU 中读阶段文档 |

## 9. 风险与应对

| 风险 | 应对 |
|---|---|
| scope 卡太长，审批人看不完 | 卡片只展示摘要 + 验收点 + DAG / diff 统计；完整内容放本地快照链接 |
| scope 展开为内部同意时出错，等于一次批了没展示的内容 | 只在 prepare 结束后出卡；展开时逐门校验 `input_hash` 在 `scope_hash` 内；审批人取各门成员策略交集 |
| prepare 段无人审批，模型连续产出错误的 Spec/DAG | GRILL 有待答问题必须停下问；每份过 schema + 前驱校验；D 卡置顶展示验收点与 DAG；不对就「需修改」重做 DESIGN 段 |
| R 卡一次授权多个 stage，审批人以为批的是"发布" | 卡片按顺序列每个 stage 做什么、是否可逆；「发布」永远是单独的 R' |
| 平台把下游人工 stage 提前显示为可操作，或回执不带身份 | 不变量 7、8；fixture 覆盖 |
| 后台 watcher 常驻旧代码，误报 `WORKFLOW_SPEC_DRIFT`（10-25 累计 8.4 万条，10-27 再次发生） | 第 1 步必做：watcher 感知代码版本 |
| 旧 run 在 RESTART 后仍自动合入或发布，新 run 重复提交 | 3.4：有未 reconcile intent 拒绝重开；旧 build 进排除集；旧 CR 人工确认处置 |
| 多仓一仓成功一仓未知，靠"C 已批准"继续 | 3.2：只认聚合结果 |
| 删掉的恢复入口在真实使用中又被需要 | 删除放到第 4 步；有 `pre-mvp` tag，按需挑回，需配真实 run 作证据 |
| 外部评审继续报出边界问题 | 按退出标准第 5 节有界评审；范围外的进改进清单 |
