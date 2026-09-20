# Tom Autodev — 可执行研发控制面

把一张 iCafe 需求卡，经澄清 → Spec → 任务拆解 → 计划/编码/评审 → 提交 iCode → iPipe 验证 → 修复 → 正式发布，端到端跑成一条**确定性、闸门化、失败即止（fail-closed）**的交付流水，所有外部副作用都压在人工审批之后，且只接受 iPipe 的真实执行证据。

> 仅在 Comate 宿主内作为控制面入口使用。项目/语言知识放在子 skill 里；没有独立的 release 组件——发布是一个由远程证据背书、经审批的终态控制器动作。

> **仓库结构**：本仓库是 tom-autodev 套件。控制面本体（约 90 个脚本、schema、references）都在 **`tom-autodev/`** 子目录下，同级目录是配套子 skill（`tom-grill` / `tom-spec` / `tom-tasks` / `tom-plan` / `tom-implement` / `tom-review` / `tom-diagnose` / `tom-lang-*` / `tom-project-*` / **`setup-tom-autodev`**）。下文所有相对路径（`scripts/`、`references/`、`schemas/`）与命令均**以 `tom-autodev/` 为基准**——即先 `cd tom-autodev/` 再执行。


---

## 目录

- [定位与硬边界](#定位与硬边界)
- [架构](#架构)
- [核心原理](#核心原理)
  - [状态机与相位](#状态机与相位)
  - [闸门 G0–G10](#闸门-g0g10)
  - [控制器相位 vs 模型相位](#控制器相位-vs-模型相位)
  - [durable-worker 驱动形态](#durable-worker-驱动形态)
  - [compute-then-approve](#compute-then-approve)
  - [证据、幂等与恢复](#证据幂等与恢复)
  - [变更类：standard / full / express / hotfix](#变更类standard--full--express--hotfix)
  - [失败处理与修复策略](#失败处理与修复策略)
  - [回执 / 缓存 / 回放（可复现性）](#回执--缓存--回放可复现性)
  - [知识 / 协作写入策略（KU + iCafe 精简）](#知识--协作写入策略ku--icafe-精简)
- [功能清单](#功能清单)
- [使用说明](#使用说明)
- [完整实例：一张 iCafe 卡跑到 RELEASE_SUCCESS](#完整实例一张-icafe-卡跑到-release_success)
- [目录结构与关键模块](#目录结构与关键模块)
- [测试](#测试)
- [参考文档](#参考文档)

## 定位与硬边界

Tom Autodev 是一套**研发控制面**：约 90 个 Python 脚本负责状态机 / 闸门 / 审批 / 幂等 / 外部副作用；一组 Markdown 子 skill 负责各相位的模型推导。它本身不写业务代码的“判断”，而是编排：读需求、归档每个相位到知识库、在如流协调研发/测试协作、驱动生成源码与产测改动、调用本地 Review、提交 iCode、监控 iPipe、路由失败、并提出受控的 G10 优化建议。

**硬边界（fail-closed）：**

- 在 Mac 本地只做：读代码/知识、生成产物、影响分析、源码 Review、worktree 管理、解析 iPipe 证据。
- 任何本地项目构建/测试请求一律返回 `LOCAL_EXECUTION_FORBIDDEN`——**只有配置好的 iPipe runner 能产生项目执行证据**。
- 绝不覆盖用户改动、绝不运行时改流水线模板、绝不持久化密钥、绝不自动合入 CR、绝不用裸 `git push` 兜底 iCode。
- 缺 profile / 独立测试仓 / Review 组件 / 稳定 iPipe profile / 环境 profile / 审批渠道时，返回 `PROJECT_NOT_READY`。

## 架构

四层，从下到上：

```
┌──────────────────────────────────────────────────────────────┐
│  子 skill（模型推导层，Markdown 提示词）                        │
│  tom-grill / tom-spec / tom-tasks / tom-plan / tom-implement   │
│  tom-review / tom-diagnose ·（语言）c-cpp/npl ·（项目）bgw/xflow│
└──────────────────────────────────────────────────────────────┘
                     ▲ 只回交 DraftContent / 证据
┌──────────────────────────────────────────────────────────────┐
│  WorkerDriver（时序层，worker_driver.py）                       │
│  advance / resume：跑完所有确定性步骤 + 控制器副作用，          │
│  只在 ApprovalJob（等人批）与 ProducerJob（等模型草案）处 park  │
└──────────────────────────────────────────────────────────────┘
                     ▲ next() 给出“下一步该干什么”
┌──────────────────────────────────────────────────────────────┐
│  确定性控制面（Python）                                         │
│  PhaseProtocol（相位契约/证据摄取）· workflow_spec（单一事实源）│
│  transition_policy · evidence_policy · repair_policy            │
│  StateStore（事件/产物/审批/锁/回执/缓存）· ApprovalLedger      │
└──────────────────────────────────────────────────────────────┘
                     ▲ 经 adapter 碰外部世界（幂等键约束）
┌──────────────────────────────────────────────────────────────┐
│  平台 adapter：iCafe · KU 知识库 · 如流 · iCode · iPipe         │
└──────────────────────────────────────────────────────────────┘
```

**关键设计取向：** 领域逻辑（相位契约 / schema / 证据策略 / 审批语义 / KU）与“谁来驱动时序”解耦。今天由 `WorkerDriver` 循环驱动、由有界 Agent 回合当 ProducerJob 的草案后端；接缝按 Temporal 的 workflow/activity/signal 三类边界标注，未来可无改动切到无头 LLM 后端或 Temporal，领域代码不动。

## 核心原理

### 状态机与相位

一次 run 是一条持久化事件序列，在下列状态间迁移（`workflow_spec.py` 是唯一事实源，`transition_policy` 只放行合法边）：

```
INTAKE → GRILL → SPEC → TASKS → WORKSPACE → PLAN → IMPLEMENT → REVIEW
       → SUBMIT → IPIPE → RELEASE → RELEASE_SUCCESS(终态)
```

分叉与回退：
- 任一相位失败 → `DIAGNOSE`（诊断），确诊后按修复方向重入 SPEC/PLAN，或升级 `ARCHITECTURE_REVIEW`。
- iPipe/发布遇环境问题 → `ENVIRONMENT_BLOCKED`，恢复后回 `IPIPE`。
- 人工终止 / 不可修复 → `STOPPED`（终态）。
- 跨仓需求：一个 REVIEW PASS 立即提交该任务，CR 可先落地，前沿重新绑定到下一个 DAG 节点，而不是等所有任务都完成才走 IPIPE。

### 闸门 G0–G10

每个 gate 都是一次**独立人工 APPROVE**，绑定到精确的 `input_hash`（Comate 与如流共享一个 `approval_id`，第一个有效回应生效）：

| 闸门 | 批的是什么 | 批准后 |
|---|---|---|
| G0 | 需求快照 + 协作绑定（群、成员、角色）| 建协作群，进入 GRILL |
| G1 | GRILL 决策日志（澄清结论、决策、验收点）| 进入 SPEC |
| G2 | Spec（行为、验收场景、测试接口、环境要求）| 进入 TASKS |
| G3 | 任务 DAG 与验收点覆盖 | 建工作区，进入 PLAN |
| G4 | 单个任务的实现计划 / 工作区绑定 | 生成业务与测试代码 |
| G5 | 候选改动集（业务 diff + 测试 diff）| 进入双轴 Review |
| G6 | 诊断结论与修复方向 | 执行修复 |
| G7 | 提交 iCode 的固定版本 **并触发 iPipe** | 开/更 CR，触发流水线 |
| G8 | 重跑失败的流水线阶段 | 按当前远程证据重跑该阶段 |
| G9 | 正式发布 | 执行发布阶段，落 RELEASE_SUCCESS |
| G10 | 控制面优化建议 | 应用优化 |

> 易错点：**初始触发 iPipe 归 G7**（“提交并触发”同一道门），**G8 只用于失败阶段重跑**；G9 才是正式发布。

### 控制器相位 vs 模型相位

- **控制器相位**（WORKSPACE / SUBMIT / IPIPE / RELEASE，及 INTAKE）：确定性、有副作用，由控制面直接执行，不需要模型。
- **模型相位**（GRILL / SPEC / TASKS / PLAN / IMPLEMENT / REVIEW / DIAGNOSE）：需要模型产出草案（DraftContent），由一次**有界 Agent 回合**填充。

worker 只在两类 job 上 park：**ApprovalJob**（等人工 APPROVE）与 **ProducerJob**（等模型草案）。

### durable-worker 驱动形态

`WorkerDriver` 拥有时序：一次 gate 结算后，worker 执行**该已批准动作**及其后所有**确定性、非副作用**步骤，直到下一个 gate 或 ProducerJob，再次 park。它消除的是“为按下一个按钮而反复唤醒 Agent”，**不是审批本身**——每个既有 gate 仍各自需要独立人工 APPROVE，worker 拒绝任何未绑定精确 `input_hash` 审批的外部副作用。

- `advance(run_id, ...)`：自动穿过确定性 transition，park 在 gate / ProducerJob / 非终态监控 / 缺运行时的控制器上。
- `submit_draft(run_id, job_id, draft)`：填充一个 ProducerJob——Agent 只交 `DraftContent`（语义正文），服务端构造 `ArtifactEnvelope`（input_hash / content_hash / 父 hash / 审批绑定，模型绝不碰元数据），再走既有 `complete-phase` 校验/发布/提交路径。
- `resume(run_id)`：消费一个已结算的审批 handoff，继续驱动（run 级隔离）。

### compute-then-approve

有副作用的控制器不凭“动作描述符”盲批，而是**先算出将要执行的精确绑定 hash，请人批这个 hash，批到了才动手**——执行的东西与批的东西不会漂移。

- SUBMIT：先派生已评审的提交描述符、算出其 `input_hash`，请 G7 批**这个**，批到才提交 iCode。
- IPIPE：先用 `trigger_input_hash` 算出触发绑定 hash，请 G7 批，批到才触发→monitor。profile 声明多个 `required_for_release` 模块时，worker **逐模块**驱动（每模块用自己的 submission 绑定、各自 G7 触发、依次 trigger→monitor→ingest），全部必需模块通过后才转 RELEASE；任一模块失败即转 DIAGNOSE，重启从未完成模块继续。
- WORKSPACE：先切好（可逆的）worktree、算出绑定 hash，请 G4 批，批到才 WORKSPACE→PLAN。worktree 切在该任务的**目标业务仓**上——DAG 节点必填 `business_module`，多仓需求按它选仓（不再恒取第一个业务仓），绑定校验强制 receipt 仓与之一致。
- RELEASE：G9 在进入 RELEASE 之后批该发布动作的 `input_hash`，批到后 worker 只读校验平台已发布锁定 build（`verify_release`）、记录发布证据、落终态；未发布则 park `RELEASE_WAITING`，不强推。

### 证据、幂等与恢复

- **EvidenceGate**：每次 checkpoint 或外部副作用前调用；input hash 变了、产物缺失、finding 未决、revision 不匹配、环境指纹陈旧、验证证据陈旧——任一即 block。
- **WorkspaceGate**：无匹配基线证据返回 `BASELINE_UNVERIFIED`。
- **幂等键** `run_id+task_id+spec_version+action+input_hash`：每个外部动作（worktree 提交、iCode submit、iPipe 触发/轮询/重跑、KU 发布、iCafe 评论、状态迁移）都带；崩溃重启后从**最后 confirmed result 事件**恢复，绝不重复提交/通知/重跑/发布。
- **单写者租约**（Phase 2）：给 `advance/resume` 注入 `LockManager` 时，worker 先取 `worker-run:<run_id>` 租约；另一个活 worker 撞上返回 `WORKER_LEASE_HELD` 而非并发竞争；崩溃持有者的租约 TTL 到期 + 死 pid 变 stale 后被下一个 worker 接管，配合事件日志 + 幂等键保证接管不重复副作用。

### 变更类：standard / full / express / hotfix

从 iCafe 卡的 `type`/`fields` **确定性映射**出建议变更类（`classify_change`），在**已有的 G0** 处由 owner 确认/覆盖（零新增闸门）。四类，产物始终齐全（spec+dag+decision-log 都在，审计/恢复不丢），差别只在模型步与闸门数：

| 类 | GRILL | SPEC+TASKS | PLAN | 何时用 |
|---|---|---|---|---|
| `standard`（默认）| 模型+G1 | **合并**：一步产 {spec,dag}、一道 G2、TASKS 免 G3 | 全 | 常规需求 |
| `full`（逃生舱）| 模型+G1 | 分开：SPEC(G2) + TASKS(G3) | 全 | 复杂/跨仓 DAG，需分别评审 |
| `express`（小改）| **auto**（卡带验收→控制器确定性产 decision-log，免 G1）| 合并（G2、免 G3）| 全 | bug/fix |
| `hotfix` | auto | 合并（G2、免 G3）| **折叠**：PLAN 仍产计划、免其 G4 | 线上热修 |

- 合并（SPEC+TASKS 一次模型步产 {spec,dag}、一道 G2）走 DraftContent 路径；`full` 是它的分开逃生舱。
- `hotfix` 的 PLAN 折叠只免 PLAN 自身的 G4；**WORKSPACE 的绑定 G4 独立强制、不受影响**。
- 任何类都**绝不跳** G5(diff)/G7(提交+触发)/G9(发布)。

### 失败处理与修复策略

- 失败先分类：`CODE_FAILURE`/`TEST_FAILURE`/确诊阻断 Review finding → 先 `tom-diagnose` 再修；环境失败绝不产生业务补丁。
- 单 run 内：同签名两轮无进展即停（`NO_PROGRESS`），三次失败修复强制架构评审，修复上限五轮。
- **跨 run FailureCase 库**：`route_failure` 按**签名**累积失败（跨 run 的出现次数、去重 run 集合、是否已解决）；`repair_policy` 签名先匹配——一个签名在 ≥2 个不同 run 里未解决地反复出现，直接升级 `ARCHITECTURE_REVIEW`（`KNOWN_CROSS_RUN_FAILURE`），而不是再修一遍。

### 回执 / 缓存 / 回放（可复现性）

- **ModelExecutionReceipt**：每次 ProducerJob 填充记录 provider/model/version/prompt_version/spec 版本/input_hash/output_hash/validators（agent-turn 后端下 model 等为空）——为回放与跨模型差分铺路。
- **草案缓存**：键 `(input_hash, prompt_version, model)`，命中即复用、不覆盖；worker 再驱动同一前沿时直接复用草案，不重新唤醒 producer。
- **golden replay + 跨模型差分门禁**（`replay_gate.py`，只读、供 CI）：`golden_replay` 校验每个回执对应的缓存草案仍哈希一致（确定性/完整性）；`cross_model_diff` 在同一输入下比对不同模型的输出，分歧则 `DIVERGENT`。
- **每 phase 不变量覆盖守卫**：`schema_validator` 里每个命名 schema 要么有语义校验器、要么显式声明为 structural-only，二者必须划分全部 schema。

### 知识 / 协作写入策略（KU + iCafe 精简）

产物**始终**存本地 `artifact_store`（正确性/恢复只读它，从不读 KU）。KU 文档与 iCafe 评论按**每相位 scope**（`workflow_spec.knowledge_scope`）精简，只在有价值处写，两处 fail-closed 校验（`_receipt_error` + `artifact_store._validate_final_envelope`）也随 scope 条件化：

| scope | 写什么 | 相位 |
|---|---|---|
| `both` | KU 文档 + iCafe 评论 | INTAKE（锚定 KU run root）、SPEC、RELEASE |
| `ku_only` | 只 KU 文档 | REVIEW、DIAGNOSE |
| `icafe_only` | 只 iCafe 里程碑评论（无 KU 子文档，评论指向 run root）| IPIPE |
| `skip` | 都不写 | GRILL、TASKS、PLAN、IMPLEMENT |

净效果：iCafe 卡只在里程碑（G0/Spec/CR/iPipe/发布）附近有评论、不再每相位刷屏；KU 只沉淀 spec/review/诊断/发布这些有复用价值的文档。


## 功能清单

- **端到端编排**：INTAKE→…→RELEASE_SUCCESS 的全相位状态机，闸门化、fail-closed。
- **worker 自驱**：确定性步骤与四个副作用控制器（WORKSPACE/SUBMIT/IPIPE/RELEASE）由 `WorkerDriver` 自动执行，确定性步骤零 Agent 唤醒。
- **审批编排**：G0–G10 双渠道（Comate + 如流）审批，hash 绑定，结算即产出可恢复 handoff；`ide_turn_hook` 让 Comate 审批后自动续跑。
- **知识沉淀**：每个相位产物经 `KnowledgeSync` 落 KU、在 iCafe 评论里挂链接。
- **iCode / iPipe 集成**：G7 提交并触发流水线，monitor 到终态；G8/人工阶段重跑（`ipipe-rerun`）；认领页面已触发的 build（`ipipe-adopt`）；平台 AI Review（小码哥）二次意见。
- **源码 Review**：`tom-review` 产出标准 + Spec 双轴证据（只出证据、不授权）。
- **失败路由与修复**：诊断→修复→限次/升级；跨 run FailureCase 复用。
- **持久化与恢复**：事件/产物/审批/锁/心跳/外部结果/handoff 全持久化；单写者租约 + 崩溃接管。
- **可复现性**：ModelExecutionReceipt + 草案缓存 + golden replay + 跨模型差分门禁。
- **G10 优化**：终态后从归档证据构建 RunSummary，提出受控的控制面自优化建议（只碰 tom-autodev 自身、拒改业务仓/profile/流水线）。

## 使用说明

> 所有命令从 `scripts/` 目录跑：`python3 orchestrator.py <子命令> ...`（也可 `python3 -m`）。控制面状态库默认在 `--config-root` 下的 `state.sqlite`。

### 0. 前置：用 setup-tom-autodev 注册项目

跑任何需求前，项目必须先经 **`setup-tom-autodev`** 子 skill 一次性注册（人工监督、只做登记，不启动 run、不生成代码、不触发 iPipe）。它产出并校验一份 project profile：

1. 在 Comate 里对目标项目调用 `setup-tom-autodev`，把要素交给它：`project_id`；业务仓路径/模块/分支/锁键；**独立**产测仓与夹具归属；语言/项目子 skill（如 `tom-lang-c-cpp` + `tom-project-bgw`）；知识源（KU）与新鲜度；外部测试接口；只读源码 Review 组件；**稳定的** iPipe 流水线 profile（预置模板 + 允许的运行时参数，不建动态阶段）；环境 profile（runner/镜像/工具版本/硬件或模拟器/数据/服务/容量，且显式声明哪些 unit/regression/integration/NCS/release 阶段在**远程**跑——无本地兜底）；Comate + 如流审批渠道与角色成员；release rule/版本映射。
2. 它跑 `project_registry.validate_profile`；缺字段则返回 `PROJECT_NOT_READY` 并列出**确切缺哪些**。
3. 校验通过后把 profile 存到 `~/.tom-autodev/config/projects/<project>.yaml`（或显式 `--config-root` 下的 `config/projects/<project>.yaml`），并记下内容 hash + 人工登记证据。密钥只留在登录文件/环境变量里，绝不读出或打印。覆盖已有 profile 需人工确认 + 提供上一份的精确 hash。
4. 只有 `READY` 才允许后续 `tom-autodev.start`。

装齐子 skill（见文末表），然后：

- 只读预检：`python3 orchestrator.py preflight <project>`——检查 iCafe 登录/版本、KU 目标可达、Comate iCode 预检、Review 组件、如流配置、iPipe discovery；不建群、不写 KU/iCafe、不提交、不触发。
- `python3 orchestrator.py acceptance-candidates <project>`——从卡片抽取验收候选（辅助定变更类）。

> 命令从 `scripts/` 目录跑：`python3 orchestrator.py <子命令> ...`。控制面状态库默认在 `--config-root` 下的 `state.sqlite`。


### 1. 启动一个 run

```bash
# 先经 iCafe 边界读快照，再启动；project 与卡号都需人工确认
python3 orchestrator.py start <requirement_id> <project>   # requirement_snapshot 经 SDK/边界传入
python3 orchestrator.py status <run_id>
```

`start` 落 INTAKE。`status` 显示当前状态并区分 `待生成` 与 `已批准待提交`。

### 2. 推进（两种方式）

**A. worker 驱动（推荐，durable-worker 形态）**——SDK/宿主里循环：

```text
advance(run_id, ipipe_api=<真实/Fake transport>, icode_runtime=..., locks=<LockManager>)
  → 若 park 在 ProducerJob：让有界 Agent 回合产 DraftContent，submit_draft(run_id, job_id, draft)
  → 若 park 在 ApprovalJob：出卡、等人批（G_n），再 advance
  → 反复，直到 RELEASE_SUCCESS
```

**B. 手动 CLI（逐相位）**：

```bash
python3 orchestrator.py next <run_id>                      # 纯、幂等地给出下一步动作
python3 orchestrator.py complete-phase <run_id> <envelope> # 提交某相位的 ArtifactEnvelope（JSON 文件路径）
```

> `resume` 只读检查点、列出待和解的外部 intent，绝不执行或完成相位：
> `python3 orchestrator.py resume <run_id>`

### 3. 审批

```bash
# 请求（给出待批 input_hash）→ 结算
python3 orchestrator.py request-approval <run_id> <action> <input_hash> ...
python3 orchestrator.py approve <approval_id> <decision> <input_hash> <channel> <run_id> <responder>
python3 orchestrator.py await-approval <run_id> <approval_id> <input_hash>
python3 orchestrator.py watch-approvals [--interval 10] [--once]   # 结算即驱动
python3 orchestrator.py reissue-approval <run_id> <action> <input_hash>
```

每张审批卡必须标明受影响仓库/模块、目标分支、锁定 revision；跨仓改动一仓一行；G7 卡还要说明是新建 CR 还是追加已有 CR。非闸门决策也要在如流问（`scripts/ask_infoflow.py`）。

### 4. iPipe 重跑 / 认领 / AI Review

```bash
python3 orchestrator.py watch-ipipe [--interval 60] [--once]
python3 orchestrator.py ipipe-rerun <run_id> <stage_build_id> <approval_id> <input_hash> --parameter NAME=VAL ...
python3 orchestrator.py ipipe-adopt <run_id> [--module M] [--window-seconds 30]  # 认领页面已触发的 build
python3 orchestrator.py ai-review start <run_id> --change-number N --revision R
python3 orchestrator.py ai-review poll  <run_id> --conversation-id C
```

失败/人工 iPipe 阶段：先读项目 skill 的运行时/参数映射，再走 `ipipe-rerun`（显式 G8/人工续跑路径，复用持久化的 stage 归属校验）。子项目 skill 从不直接调 iPipe。

### 5. 恢复 / profile 重钉

```bash
python3 orchestrator.py recover-rebuilt-change-set <run_id> <task_id> <plan_artifact_id>
python3 orchestrator.py repin-profile <run_id> <previous> [--request] [--approval-id ID]
```

### 6. 终态后优化（G10）

```bash
python3 orchestrator.py optimize <run_id> build
python3 orchestrator.py optimize <run_id> propose --summary ... --allowed-root <tom-autodev 根>
python3 orchestrator.py optimize <run_id> apply --proposal-id P --approval-id <G10 approval>
```

只有从归档 run 证据生成的提案可被应用，G10 绑定候选 hash，目标限 tom-autodev 自身根目录，拒改业务仓/profile/流水线；校验失败则逐文件回滚。

### 子 skill

| 相位 | Skill |
|---|---|
| 澄清 / Spec / DAG | `tom-grill`、`tom-spec`、`tom-tasks` |
| 计划 / 编码 / 评审 | `tom-plan`、`tom-implement`、`tom-review` |
| 诊断 | `tom-diagnose` |
| 语言 | `tom-lang-c-cpp` 或 `tom-lang-npl` |
| 项目 | `tom-project-bgw` 或 `tom-project-xflow` |

项目 skill 提供仓库拓扑、环境、iPipe 参数名、日志位置、版本/计数检查；本控制面负责 `next` / G8 / `ipipe-rerun` / 证据摄取的时序。每个子相位遵循 `references/phase-protocol.md`：消费并产出 schema 校验、内容哈希的 `ArtifactEnvelope`，把变更文档落 KU，并在 iCafe 评论里挂链接；子 skill 只回交产物与证据，只有本控制面拥有 iCafe/KU/iCode/iPipe adapter 与 G0–G10 闸门。

## 完整实例：一张 iCafe 卡跑到 RELEASE_SUCCESS

以 BGW 项目、卡 `BGW-1956`（`type=feature`，标准需求）为例，串起从需求到发布的全过程。约定：`orch = python3 orchestrator.py`（在 `scripts/` 下）。`WD` 指 SDK/宿主里对 `worker_driver` 的调用。

### 1) 从 iCafe 到启动 run

```bash
orch preflight bgw                 # 只读预检，全绿才继续
orch acceptance-candidates bgw     # 看卡片验收候选，帮 owner 在 G0 定变更类
```

经 iCafe 边界读到卡快照后启动（`type=feature` → `classify_change` 建议 `standard`，owner 在 G0 可覆盖成 `full`/`hotfix`/`express`）：

```bash
orch start BGW-1956 bgw            # 落 INTAKE，返回 run_id（下称 $RUN）
orch status $RUN                   # 看到 state=INTAKE
```

### 2) 启动 worker，让它自己推进

worker 拥有时序：一次 `advance` 会跑完所有确定性步骤 + 四个副作用控制器（WORKSPACE/SUBMIT/IPIPE/RELEASE），**只在两处 park**——需要人批的 **ApprovalJob**、需要模型草案的 **ProducerJob**。SDK/宿主里循环：

```python
res = worker_driver.advance(orch, run_id,
        knowledge_sync=ks, icode_runtime=icode, ipipe_api=ipipe,
        locks=locks, owner_token="worker-A")   # locks 见第 8 节
```

`advance` 返回时 `res["parked"]` 告诉你停在哪：`APPROVAL_WAIT`（等某个 gate）/ `PRODUCER_WAIT`（等某相位的模型草案）/ 终态。

### 3) park 在 ProducerJob → 切到 Comate Agent 填草案 → 切回 worker

当 `advance` 停在 `PRODUCER_WAIT`（比如 GRILL/SPEC/PLAN/IMPLEMENT/REVIEW 需要模型产出）：

- **切到 Comate Agent**：由一次**有界 Agent 回合**读取该 ProducerJob（钉住 input_hash + prompt_version + 目标 schema），只产出符合该相位 schema 的 `DraftContent`（语义正文，比如 spec 的正文、change-set 的 diff）。**Agent 只产内容，绝不碰元数据、绝不跑控制器/complete-phase。**
- **回交草案**：`worker_driver.submit_draft(orch, run_id, job_id, draft, knowledge_sync=ks)`。服务端据此构造 `ArtifactEnvelope`（input_hash/content_hash/父 hash/审批绑定），跑校验/发布/存产物；若该相位有闸门（如 SPEC 的 G2）且未批，返回 `APPROVAL_REQUIRED` 并给出待批 hash。
- **切回 worker**：草案落定后再调 `advance`，worker 继续确定性推进到下一个 park 点。

> `standard` 下 SPEC 是**合并**相位：一次 ProducerJob 产 `{spec, dag}` 两份草案、一道 G2；worker 随后自动写 dag、TASKS 免 G3。`hotfix` 下 PLAN 折叠（仍产计划、免其 G4）。

### 4) 审批各 gate（compute-then-approve）

当 `advance` 停在 `APPROVAL_WAIT`，`res` 带出 `gate` 与要批的 `approval_input_hash`（有副作用的控制器是"先算出将执行的精确 hash 再请人批"）。owner 在 Comate 或如流批（二者共享一个 `approval_id`，第一个有效回应生效）：

```bash
orch approve <approval_id> APPROVE <input_hash> comate $RUN <responder>
```

批完再 `advance`，worker 拿着这笔审批执行对应动作（切 worktree / 提交 iCode / 触发 iPipe / 记发布证据）。整条 standard 路径的人工闸门：G0→G1→G2→G4(工作区)→G4(计划)→G5→G7(提交+触发)→G7(iPipe 触发)→G9。

> 如果配了 Comate 的 `Stop` hook（`scripts/ide_turn_hook.py`），审批结算后会自动续跑，无需手动"继续"。

### 5) 查询工作流当前状态

```bash
orch status $RUN     # 当前 state；区分 待生成(ProducerJob) vs 已批准待提交
orch next $RUN       # 纯、幂等地给出"下一步该干什么"（不产生副作用）
orch trace $RUN      # 事件序列、产物、协作/审批、角色路由——全貌
```

### 6) 继续一个（park 住的）工作流

- **审批后继续**：审批结算会写一条绑定的 resume handoff；`worker_driver.resume(orch, run_id, ...)` 消费它并驱动前进（run 级隔离，只认本 run 的 handoff）。或直接再调 `advance`。
- **崩溃/中断后继续**：`orch resume $RUN` 只读检查点、列出待和解的外部 intent（**它不执行、不完成相位**）；和解干净后再 `advance`——幂等键保证不重复提交/触发/发布。

### 7) 修改一个工作流

不是"直接编辑状态"，而是**沿状态机重入**：

- 改设计：失败或需求变更 → 进 `DIAGNOSE` 确诊（G6）→ 按修复方向重入 `SPEC`/`PLAN` 重新产草案、重新过闸门。需求本身变了则重入 `GRILL`。
- 改代码：REVIEW 未过 / iPipe 失败 → `route_failure` 归类 → `tom-diagnose` → 修复落成同一 CR 的新 revision → 重新提测。
- profile 变了：`orch repin-profile $RUN <previous> --request` 开 `PROFILE_REPIN` 门重钉。
- 换 input 就要换审批：hash 一变，旧审批自动失效、要重新批（`reissue-approval`）。

### 8) 多个工作流同时跑：会有什么问题、怎么处理

多个 run 各自独立（run_id 隔离事件/产物/审批/幂等键）。真正的风险是**同一个 run 被两个 worker 同时驱动**（重复副作用、状态竞争）——比如 Stop-hook 自动续跑和手动 `advance` 撞车。处理：

- **单写者租约**：给 `advance/resume` 注入 `LockManager`，worker 先取 `worker-run:<run_id>` 租约再驱动、返回即释放。另一个活 worker 撞上直接返回 `WORKER_LEASE_HELD`，**不并发竞争**：

  ```python
  worker_driver.advance(orch, run_id, ..., locks=locks, owner_token="worker-B")
  # -> {"ok": False, "reason_code": "WORKER_LEASE_HELD", "owner_pid": ...} 若被 A 持有
  ```

- **崩溃接管**：持有者崩了，其租约 TTL 到期 + 死 pid 变 stale 后，下一个 worker 自动接管；配合事件日志 + 幂等键，接管**不重复**已确认的副作用。
- **不同 run 并行**：无需互斥，各跑各的；共享的只有审批人的注意力——每张审批卡都标明仓库/模块/分支/revision，避免批错对象。

## 目录结构与关键模块

```
tom-autodev/
├── SKILL.md                     # Comate 入口的操作契约（宿主读这份）
├── README.md                    # 本文档
├── CHANGELOG.md                 # 发布记录
├── schemas/                     # 各产物 JSON Schema（含 ipipe-evidence / release-evidence）
├── references/                  # state-machine / phase-protocol / approval-policy / failure-taxonomy / project-registry
└── scripts/
    ├── orchestrator.py          # 控制面 + CLI 子命令入口
    ├── workflow_spec.py         # 状态/闸门/迁移/文案的单一事实源（workflow-spec-v1）
    ├── worker_driver.py         # WorkerDriver：advance / resume / submit_draft + 控制器执行 + 单写者租约
    ├── phase_protocol.py        # 相位契约、证据摄取（含 ingest_ipipe_evidence / ingest_release_evidence）
    ├── transition_policy.py     # 合法迁移边
    ├── evidence_policy.py       # 入口证据/闸门/revision/环境要求
    ├── repair_policy.py         # 修复决策 + 跨 run 签名匹配
    ├── replay_gate.py           # golden replay + 跨模型差分门禁（只读）
    ├── schema_validator.py      # schema + 语义不变量 + 覆盖守卫
    ├── state_store.py           # 事件/产物/审批/锁/回执/草案缓存/FailureCase 持久化
    ├── lock_manager.py          # 锁/租约（acquire/heartbeat/release/接管）
    ├── recovery.py              # 检查点检查（只读，指向 WorkerDriver）
    ├── clients/                 # iCode / iPipe / KU 等平台 adapter（ipipe_runtime 等）
    └── tests/                   # 控制面回归测试
```

## 测试

```bash
cd scripts/tests && python3 -m unittest discover -s . -p "test_*.py"
```

当前 **806** 项全绿（含一条“单 standard 需求由 WorkerDriver 全程驱到 RELEASE_SUCCESS”的验收用例：断言 ProducerJob 次数=模型相位数、四个副作用控制器全在 worker 循环内自动执行、每道闸门一次人工 APPROVE、事件数远低于历史真实 run 的 102）。用纯标准库 `unittest`（不用 pytest）；平台交互全部对 Fake adapter 验证，生产真实 transport 经 `orchestrator._cli_ipipe_runtime` 注入。

## 参考文档

- `references/state-machine.md` — 迁移与恢复
- `references/phase-artifacts.md` — 相位契约
- `references/phase-protocol.md` — 相位执行契约（WorkerDriver 拥有时序）
- `references/approval-policy.md` — 审批策略（请求审批前读）
- `references/failure-taxonomy.md` — 失败分类
- `references/project-registry.md` — 项目注册与选择

> 本 skill 属于 tom-autodev 套件（共 13 个），需一起安装才能完成端到端流程。





