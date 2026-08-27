# Tom Autodev 可执行控制面设计

**日期：** 2026-08-10  
**状态：** 已完成对话设计确认，等待书面 Spec 复核  
**范围：** `tom-autodev`、阶段 Skill、语言/项目 Skill、控制面脚本及其平台适配器

## 1. 目标

将 `tom-autodev` 从控制面原型升级为面向 Comate 的可执行需求交付闭环。需求从 iCafe 进入，经需求澄清、Spec、Task DAG、Task Plan、研发/测试代码生成、本地源码 Review、iCode、iPipe、失败诊断与修复，直到发布证据验证完成。

本机 Mac 只负责读取代码和知识、生成业务及测试代码、管理隔离 worktree、执行源码 Review、解析远端证据。项目编译、单测、回归、集成、模拟器、Docker/NCS 和发布执行只能发生在批准的 iPipe 环境中。

## 2. 核心决策

1. 采用模块化控制面，不引入独立工作流服务。
2. Python 运行时负责确定性状态、证据、审批、外部平台调用、幂等和恢复。
3. Comate 宿主 Agent 按控制面返回的 `next-action` 加载对应阶段 Skill，生成结构化产物并回交控制面。
4. 生产适配器必须调用真实 iCafe、知识库、如流、iCode 和 iPipe 能力；fake transport 仅用于控制面测试。
5. 所有状态转换 fail-closed。缺少证据、hash、审批、revision、环境指纹或远端回执时不得推进。
6. 每个阶段产物保存为知识库中的不可变子文档，并以 URL、版本和 hash 回写 iCafe 评论。
7. 每个需求可创建研发测试协作群。环境和测试问题路由给测试，开发问题路由给研发，混合问题同时通知双方。
8. 每次 run 结束生成运行总结和优化提案；只有 G10 批准具体 diff 后才允许优化 Skill。

## 3. 总体架构

```text
iCafe Requirement
    |
    v
RunController ---- ProjectRegistry / CapabilityDiscovery
    |                         |
    |                         +---- BGW / C-C++ profile and rules
    |                         +---- XFlow / NPL profile and rules
    |
    +---- StateStore / ArtifactStore / ApprovalLedger
    +---- EvidencePolicy / TransitionPolicy / RepairPolicy
    +---- WorkspaceManager / LockManager / Heartbeat
    +---- CollaborationSession
    +---- PhaseProtocol <----> Comate phase executor
    +---- PlatformAdapters
             +---- iCafe CLI
             +---- KU CLI
             +---- Infoflow group scripts
             +---- embedded Infoflow approval/wait gateway
             +---- local code-review Skill
             +---- iCode CLI
             +---- embedded iPipe monitor/retry/release verifier
```

控制面提供以下命令：

```text
start
next
complete-phase
approve
resume
status
stop
optimize
```

`next` 每次只返回一个允许执行的动作、固定输入产物、对应 Skill 和完成契约。`complete-phase` 只接受通过专用 Schema、hash、基线、知识库回执和审批检查的结果。阶段执行只支持 Comate，不维护 Codex 兼容入口。

原 `tom-autorelease` 不作为运行时组件或依赖保留。其可复用实现迁入本 Skill：

- iCode preflight、CR 提交、revision/module/branch 解析和提交幂等；
- iPipe pipeline/build/stage/job 查询、触发、监控、失败日志采集、stage 重跑和 release 校验；
- Infoflow approval wait、request/reply、heartbeat、失败通知和停止通知；
- module lock、attempt budget、状态文件和跨进程恢复。

迁移后 `tom-autodev` 的所有生产调用只引用自身 `scripts/`、`clients/` 和 `infoflow-gateway/`，不读取或执行 `/Users/tom/Desktop/skills/tom-autorelease`。

## 4. 项目注册与自动发现

统一使用：

```text
~/.tom-autodev/config/projects/<project>.yaml
```

profile 必须包含：

- 项目标识、语言 Skill、项目 Skill；
- 业务仓库路径、iCode module、目标分支、锁 key；
- 独立产品测试仓库；
- 知识来源、知识图谱、revision 和 freshness 规则；
- 本地 source-only Review provider；
- iPipe pipeline ID、允许参数、阶段分类和发布规则；
- runner、OS/arch、镜像及工具链 digest、硬件/模拟器、数据、服务和容量；
- Comate、如流和角色审批配置；
- 研发、测试、项目负责人完整邮箱及 iCafe 字段映射；
- 知识库 repo ID 和父文档 ID。

BGW 知识库目标：

```text
repo_id: sX0BTOBWJX
parent_doc_id: I15ClP2KW4ZGAK
```

XFlow 知识库目标：

```text
repo_id: sX0BTOBWJX
parent_doc_id: meQ-Acjg0K09Xr
```

`setup-tom-autodev` 通过仓库 remote、iCode 和 iPipe 查询发现候选配置，输出 readiness 报告。只有候选唯一并经人工确认后才能写入 profile。不得从目录名猜测试仓库、流水线或邮箱，不得将凭据写入 profile。

profile 校验必须使用 JSON Schema，并执行路径、Git、Skill、provider、双通道、iPipe 参数 allowlist、环境能力和发布规则深度检查。

## 5. iCafe 契约

生产适配器使用 `/Users/tom/.comate/skills/.system/icafe` 提供的 `icafe-cli`。

1. 会话首次调用执行 `icafe-cli version` 和登录检查。
2. 明确卡片号时调用 `card get --space <space> --sequence <sequence> --brief`。
3. 未明确卡片时使用 `card smart-find`，不能跳过已有卡片搜索直接创建。
4. `RequirementSnapshot` 保存标题、正文、验收点、字段、附件/链接、状态、修改时间和 canonical content hash。
5. iCafe 正文只作为不可变快照读取。阶段结果通过评论回写，不自动覆盖正文。
6. 状态更新前调用 `card next-statuses`，只使用 profile 映射中的合法状态。
7. iCafe CLI 不支持物理删除。删除语义为：查询当前状态、人工确认、流转到取消/关闭，并追加原因和知识库 URL。不可达时返回 `ICAFE_DELETE_UNSUPPORTED`。
8. 每个远端写操作保存 intent、响应、远端对象 ID 和幂等 key。

## 6. 知识库契约

生产适配器使用 `/Users/tom/.comate/skills/.system/ku-doc-manage` 提供的 `ku` CLI。

每个 run 在项目父文档下创建：

```text
<iCafe 卡片>-<需求摘要>-研发测试协作
├── 00-requirement-snapshot
├── 01-grill
├── 02-spec
├── 03-tasks
├── 04-task-plan/<task-id>
├── 05-change-set/<task-id>
├── 06-review/<task-id>
├── 07-diagnosis/<attempt>
├── 08-ipipe-evidence/<build-id>
├── 09-run-summary
└── 10-optimization-proposal
```

阶段产物每次生成新文档，不覆盖历史文档。根文档只维护索引；更新前读取当前内容，使用 `edit-content` 的精确局部编辑，成功后必须调用 `publish-doc`，然后重新查询确认远端内容和版本。

知识库写入成功的判定包括：CLI 退出码、业务返回码、doc ID、repo ID、URL、重新查询结果和内容 hash。任何一项缺失均不得将产物标记为已发布。

## 7. 研发测试协作会话

每个 run 在 G0 后创建 `CollaborationSession`：

```text
group_id
group_name
group_owner
dev_members
test_members
owner_members
member_snapshot
bot_id
message_receipts
approval_requests
last_heartbeat
```

成员解析顺序：

1. profile 中固定的研发、测试和项目负责人成员；
2. profile 允许时追加 iCafe 卡片负责人；
3. 根据已配置字段映射读取 iCafe 的研发/测试负责人；
4. 不能解析为完整邮箱时进入 `MEMBER_CONFIRMATION_REQUIRED`。

G0 必须展示并批准群名、群主和成员清单。群名模板为：

```text
<project>-<icafe-card>-<short-title>-研发测试协作
```

组合适配如下：

- `infoflow-message-group`：建群、成员管理、群公告、群消息和 @ 指定成员；
- 迁入 `tom-autodev` 的 Infoflow gateway：长时间等待回复、人工审批和 iPipe 监控回调。

消息责任路由：

| 分类 | 默认责任角色 |
|---|---|
| 环境、测试数据、测试用例、回归/集成断言 | 测试 |
| 代码、接口、Spec/Task Plan 偏差、Review finding | 研发 |
| 代码与测试边界不清 | 研发和测试 |
| 权限、平台、发布规则 | 项目负责人 |

带 @ 的消息使用 MD 类型，content 和 `atUsers` 必须同时包含目标用户。消息包含 iCafe、知识库、revision、pipeline/build/stage/job、失败摘要和下一步动作。

群回复只唤醒控制面并形成审计证据，不能绕过 G6、G8 或 G9。控制面必须重新读取代码、文档和 iPipe 状态，确认新证据后才能继续。

## 8. 阶段协议

所有阶段产物使用统一 `ArtifactEnvelope`：

```json
{
  "run_id": "string",
  "phase": "string",
  "task_id": "string-or-null",
  "schema_version": "1",
  "input_hash": "sha256",
  "content_hash": "sha256",
  "source_revisions": {},
  "parent_artifact_hash": "sha256",
  "knowledge_doc_id": "string",
  "knowledge_url": "string",
  "evidence_refs": [],
  "approval_id": "string-or-null"
}
```

### 8.1 Grill

输入 iCafe 快照、代码/知识证据和已有决策。先查事实，再构建设计决策树。按依赖 frontier 提问，维护 Decision Log、glossary delta 和必要的 ADR candidate。事实不得询问用户；术语与代码冲突必须显式指出。无开放决策时返回 `NO_OPEN_DECISIONS`。

### 8.2 Spec

产出编号行为、Given/When/Then 验收场景、边界和异常、兼容性、测试接口、环境要求、非目标、风险、回滚和 release evidence。每个 iCafe 验收点必须映射 Spec behavior 和产品测试计划。

### 8.3 Tasks

按独立可验证的端到端 capability slice 构造 DAG。每个 task 同时包含业务仓库和独立测试仓库变更、测试 ID、fixture、iPipe 阶段和完成谓词。校验 DAG 无环、验收覆盖完整、无未映射 task。

### 8.4 Plan

每个 frontier task 产出精确仓库、文件、模块、符号、接口、测试 ID、fixture、断言、iPipe 参数、风险、回滚和逐步 checklist。禁止模糊动作和未定义名称。

### 8.5 Implement

先生成测试意图、fixture 和测试代码，再生成最小业务代码。业务和测试变更属于同一 `change_set_id`。只能在 task-owned worktree 中修改，不能执行本地项目编译或测试。

### 8.6 Review

宿主 Agent 加载 `/Users/tom/.comate/skills/.system/code-review`，固定同一 diff baseline，执行 Standards 和 Spec 两轴 Review。每条 finding 包含严重级别、路径/符号/行、证据、验收点和 blocking 标志。阻塞 finding 进入 Diagnose，不能直接编辑代码。

### 8.7 Diagnose

冻结 revision、test revision、pipeline/build/stage/job、环境指纹和失败日志。区分代码、测试、环境、pipeline transient、revision mismatch 和发布平台问题。只提出一个可证伪根因假设；证据不足返回 `DIAGNOSIS_INCOMPLETE`。

## 9. 语言和项目规则

### 9.1 C/C++

Review 和生成规则至少覆盖：

- ABI/API、调用方和序列化兼容性；
- 所有权、生命周期、nullability、整数和 buffer 边界；
- 错误传播、资源清理和异常路径；
- 并发、锁顺序、共享状态和线程安全；
- 平台、编译器和未定义行为假设；
- 业务行为与产品测试断言一致性。

### 9.2 NPL

Review 和生成规则至少覆盖：

- construct 类型及合法字段；
- stage、transition、bus 方向、producer/consumer；
- field width、offset、validity 和资源预算；
- malformed、truncated、unmatched、drop、trap、mirror 和 error 行为；
- table 类型、key/result width、容量及 update/lookup 限制；
- 当前 XFlow 源码、批准文档和 read-only CNA 示例的证据优先级。

NPL 不能以 C/C++ 类比替代语义证据。编译器和芯片限制只能由匹配 revision 与环境指纹的 iPipe 证据确认。

## 10. G0-G10 审批

| Gate | 对象 |
|---|---|
| G0 | iCafe 卡片、项目、协作群和成员清单 |
| G1 | Grill/架构决策 |
| G2 | Spec、测试接口、环境要求 |
| G3 | 完整 Task DAG |
| G4 | 每个 Task Plan |
| G5 | 每个完整候选 diff |
| G6 | Diagnosis、修复方向、修复计划和修复 diff |
| G7 | iCode submission/patchset |
| G8 | iPipe 失败阶段重跑或人工阶段继续 |
| G9 | release evidence 和发布动作 |
| G10 | Skill 优化候选 diff |

审批绑定 canonical input hash。Comate 和如流使用同一 approval ID，第一份有效响应生效，后续响应只审计不改结果。输入变化立即使审批过期。超时停止 run 并生成 handoff。

## 11. Workspace、锁和恢复

每个 task/repo 创建独立 Git worktree，保存：

```text
run_id / task_id / repo
baseline revision
branch
worktree path
lock key
owner token
created_at
last_heartbeat
ttl
user change snapshot
```

禁止在用户原工作区直接实现。检测到用户脏改动时记录但不移动、不 stash、不覆盖。锁必须通过原子创建获得；旧锁只能在 owner 进程不存在且 TTL/heartbeat 过期后，经明确恢复流程接管。

恢复基于已提交事件、远端回执和 checkpoint。`resume` 必须重新查询不确定的外部状态，不得重复建群、建文档、评论、提交、触发、重跑或发布。

## 12. 外部调用和幂等

所有副作用遵循：

```text
intent persisted -> external call -> receipt persisted
-> evidence hash -> legal transition
```

生产适配器：

- iCafe：`icafe-cli`；
- KU：`ku`；
- 如流群管理：`infoflow-message-group/scripts`；
- 如流审批等待：迁入 `tom-autodev` 的 Infoflow gateway；
- Review：Comate system `code-review` Skill；
- iCode：preflight + `icode-cli git push_cr` 和 API 查询；
- iPipe：迁入 `tom-autodev` 的 IPipeClient/monitor/retry/release verifier，协议参考 `ipipe-pipeline-assistant`。

网络超时或结果未知时必须先查询远端对象和幂等 key。认证、权限、QPS、业务返回码和响应 Schema 失败均返回明确 reason code，不能静默跳过或切换 fake。

## 13. 状态和失败路由

主路径：

```text
INTAKE -> COLLABORATION -> GRILL -> SPEC -> TASKS
-> WORKSPACE -> PLAN -> IMPLEMENT -> REVIEW
-> SUBMIT -> IPIPE -> RELEASE -> RELEASE_SUCCESS
-> RUN_SUMMARY -> OPTIMIZATION_PENDING -> COMPLETE
```

失败路由：

- requirement changed：回到 GRILL 并使下游审批失效；
- code/test confirmed failure：DIAGNOSE -> SPEC/PLAN/IMPLEMENT；
- environment unsatisfied：ENVIRONMENT_BLOCKED，@测试，不生成业务 patch；
- pipeline transient：记录证据，G8 后重跑；
- revision mismatch：丢弃证据并停止；
- review incomplete：停止并请求 provider/human review；
- external auth/permission：停止并通知项目负责人。

相同无进展失败签名出现两次停止自动建议；三次批准修复失败进入 architecture review；五轮 repair 停止 task。

## 14. 环境与测试契约

G2 批准的环境包含 runner OS/arch、镜像和工具链 digest、硬件/模拟器、数据、服务、容量和 pipeline revision。iPipe evidence 必须携带相同环境指纹；不一致时结果无效。

测试用例由以下上下文生成：

```text
iCafe acceptance
+ Grill decisions
+ Spec behaviors
+ current code and impact graph
+ project/language knowledge
+ existing test precedents
+ approved environment capability
```

每个测试必须包含 `test_id`、验收点、fixture、独立预期结果来源、执行阶段和环境要求，并提交到独立产品测试仓库。研发和测试共同评审测试接口、用例和预期结果。执行证据只来自 iPipe。

## 15. G10 自优化

每次 run 结束生成 `RunSummary`，记录阶段耗时、人工等待、重试、Review finding、iPipe 失败签名、责任分类、解决路径、缺失知识和重复人工操作。

`OptimizationProposal` 每项包含：

```text
evidence
root cause
target Skill/schema/script
candidate diff
expected benefit
risk
rollback
verification commands
```

G10 只允许修改 `tom-autodev` 家族 Skill、控制面脚本、Schema、模板、测试和非敏感 profile 规则候选。禁止自动修改业务代码、产品测试代码、iPipe 配置、iCafe 原始正文、历史阶段文档、凭据、权限和群成员配置。

批准必须绑定候选 diff hash。验证失败时回滚本次优化，不继续叠加修改，并保存失败报告。

## 16. 验收策略

### 16.1 控制面测试

- EvidencePolicy 空证据 fail-closed；
- 每个状态只接受固定证据和固定审批；
- route failure 不能绕过 transition policy；
- approval timeout、失效、冲突和晚到响应可审计；
- worktree、锁、heartbeat、ownership 和恢复；
- artifact/schema/hash/knowledge receipt；
- 外部 intent/result 幂等和崩溃恢复。

### 16.2 Adapter contract 测试

覆盖 iCafe、KU、如流、Review、iCode 和 iPipe 的成功、业务失败、认证、权限、超时、QPS、重复调用和不确定结果。

### 16.3 Fake E2E

BGW 和 XFlow 各覆盖：

- 完整成功路径；
- code failure -> @研发 -> repair；
- test failure -> @测试 -> test repair；
- environment failure -> @测试 -> environment unblock；
- review failure；
- crash/resume；
- duplicate callback；
- G10 approve/reject。

### 16.4 Live 验收

先执行只读 preflight，验证真实登录、知识库目录、仓库、Review provider 和 pipeline discovery。之后必须经人工批准，在非生产 iPipe 中分别执行一次 BGW 和 XFlow dry-run。生产启用前必须满足：

- 无空证据推进；
- 无重复外部副作用；
- 崩溃后可恢复；
- 研发/测试责任路由正确；
- 环境指纹和 revision 严格匹配；
- Mac 未执行任何项目编译或测试。

## 17. 开源规则来源

通用阶段规则参考并适配：

- `mattpocock/skills`：grilling、domain-modeling、ADR/glossary 和 handoff；核对 revision `84fdeffd12f2ee307994d1eb6feb48173b6e0502`。
- `github/spec-kit`：clarify、spec、plan、tasks、analyze 和 implement 的覆盖与一致性检查；核对 revision `684b3d8e05263a7c1948d3d0699ab1cb4f77c3d5`。
- `obra/superpowers`：design-before-code、TDD、worktree、systematic debugging、requesting/receiving review 和 verification-before-completion。

开源规则只用于通用工程流程。BGW、C/C++、XFlow 和 NPL 的事实必须来自当前 revision 的代码、项目规则、批准知识库和匹配环境的 iPipe 证据。

## 18. 非目标

- 不在本机运行 BGW/XFlow 的编译、单测、回归、集成、Docker、NCS 或模拟器。
- 不自动创建或修改 iPipe 模板。
- 不自动合入 iCode CR 或绕过人工评审。
- 不用 LLM 猜测平台配置、成员邮箱、测试仓库或环境能力。
- 不将群消息或人工回复当作执行证据。
- 不在没有 G10 的情况下自我修改。
