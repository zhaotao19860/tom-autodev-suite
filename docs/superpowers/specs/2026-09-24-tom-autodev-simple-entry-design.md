# Tom Autodev 简化入口与耐久续跑设计

日期：2026-09-24  
状态：待用户审阅  
范围：`tom-autodev-suite` 控制面和 Comate `/tom-autodev` 使用体验

## 1. 背景与目标

Tom Autodev 已有可恢复状态、审批台账、handoff 和 `worker_driver`，但公开给 Agent/使用者的接口没有完整接通：CLI 的 `start` 只创建 run，CLI 的 `resume` 只读恢复检查点；`worker_driver.advance/resume/submit_draft` 是 Python API，没有对应的 Agent-facing adapter 或 CLI 子命令。Skill 描述了 `submit-draft`，而运维文档说明它不是当前 CLI 命令。日常流程因此要求 Agent 理解内部阶段和接口边界，维护人员也要在多个操作间切换。

本设计把 `/tom-autodev` 作为唯一日常入口，提供清晰的运行、查看、继续和停止交互，并让现有 WorkerDriver 成为实际的阶段推进者。用户只处理明确的需求澄清、审批、阻塞决定和交付确认；Agent 只生成当前 ProducerJob 的内容；worker 继续负责校验、状态推进、审批与平台副作用。

## 2. 设计约束

- 保持 Comate 为唯一 Agent 入口，沿用现有项目 profile、SQLite 状态、ArtifactStore、ApprovalLedger、WorkerDriver 和平台 adapter。
- 保留 G0–G10、精确 `input_hash` 绑定、EvidenceGate、工作区所有权、revision/environment pin、iPipe 证据和外部副作用恢复规则。
- 不允许模型自行创建 ArtifactEnvelope、批准记录、状态迁移或平台回执；模型只提交当前任务要求的 DraftContent。
- 不改变本机与远程执行边界；项目编译、测试、模拟器和发布仍由配置的 iPipe 执行。
- 把复杂恢复与排障操作保留给维护者；普通用户无需接触内部 Python API、事件 ID、artifact ID 或 hash。
- 不引入 Temporal Server、独立 Web 工作台或第二套持久化状态。

## 3. 使用者交互

### 3.1 日常意图

`/tom-autodev` 接受自然语言或显式动作：

| 意图 | 行为 |
|---|---|
| 处理需求卡 | 验证用户确认的项目和 iCafe 卡，复用已 READY 的 profile，创建或定位唯一 run，然后驱动至下一个需要模型、审批或外部证据的位置 |
| 查看进度 | 按卡号或 run 定位；展示阶段、状态、下一责任方、待办/审批和阻塞原因 |
| 继续 | 对唯一匹配的 run 消费有效 handoff 或从持久化检查点继续驱动；不重新生成已保存的草案，不重复未确认的外部写入 |
| 停止 | 对明确选择的 run 记录停止请求；不删除事件、产物、审批或工作区 |

项目和卡片仍须得到用户确认。若卡片可匹配多个项目或有多个活动 run，入口先展示候选并要求选择，不从当前目录、聊天猜测或最近使用记录静默绑定。

### 3.2 默认进度卡片

`status` 与 `/tom-autodev` 的运行摘要使用同一套状态投影，默认只显示：

- 需求卡、项目和当前阶段；
- `待生成`、`待审批`、`等待 iPipe`、`待用户决定`、`已阻塞`、`已完成` 等人能理解的状态；
- 当前责任方和下一步；
- 需要用户处理时的具体对象、版本和动作；
- 有阻塞时的可执行下一步。

内部 ID、hash、详细事件和 adapter 错误只在维护/诊断视图中展开。不得用摘要状态代替底层 gate 判断。

### 3.3 普通运行与维护接口

普通用户的稳定动作限于 `start`、`status`、`continue`、`stop`。高级命令如 `advance`、`complete-phase`、各类 `recover-*`、`ipipe-rerun`、`repin-profile` 留在维护文档并明确用途。`resume` 的现有只读语义保持不变，避免破坏依赖它的恢复流程；真实运行续跑使用新的 WorkerDriver 入口。

## 4. 运行时架构

新增薄的 AgentBridge（名称可在实现计划中按现有风格调整），作为 `/tom-autodev`、CLI/Comate host 和 WorkerDriver 之间的装配边界。它复用已有 Orchestrator 和 adapter 构造函数，不复制 workflow 规则。

```text
Comate /tom-autodev
       │ start / status / continue / stop
       ▼
AgentBridge ── assembles Orchestrator + configured adapters + run lock
       │
       ├── WorkerDriver.advance / resume
       │      ├── deterministic controller work + EvidenceGate
       │      ├── park on ApprovalJob
       │      └── park on ProducerJob ── Comate skill generates DraftContent
       │                                  │
       └──────────────── submit_draft ◀──┘
                       │
                       └── validate, persist, bind approval, continue
```

### 4.1 Bridge operations

- `start(requirement_id, project, confirmed_change_class?)`: 保持当前快照读取、profile 检查和幂等启动行为；返回 run 摘要与 G0/下一动作，不绕过 G0。
- `drive(run_id)`: 在 run 级锁保护下调用 `worker_driver.advance`，执行 worker 拥有的确定性动作，直至 ProducerJob、ApprovalJob、远程等待、终态或阻塞。
- `submit_draft(run_id, job_id, draft_content)`: 只接受当前动作的 schema 内容，调用 `worker_driver.submit_draft`。Bridge/worker 负责构造 envelope、校验、持久化和准确的审批绑定。
- `continue_run(run_id)`: 存在有效批准 handoff 时调用 worker resume，否则由当前 checkpoint 安全 drive；拒绝过期审批、重复动作和不确定的外部 intent。
- `status(run_id or requirement_id)`: 输出共享的紧凑状态摘要；多个候选时要求选择，不静默选取。
- `stop(run_id)`: 调用现有停止合同并返回保留的检查点。

CLI 可以提供薄包装用于宿主和排障，例如 `drive`、`submit-draft`、`continue`；CLI 不负责生成模型内容，也不能把 `complete-phase` 当作子 Skill 的正常接口。用户日常仍通过 `/tom-autodev` 使用这些能力。

### 4.2 每轮工作边界

1. Bridge/worker 读取当前 `next()` 动作并从持久化记录恢复已保存结果。
2. Worker 执行无须判断且已满足约束的步骤；到达需要模型内容时持久化/返回一个 ProducerJob。
3. 当前 Comate turn 按 ProducerJob 的 schema、输入 artifact 和适用 skill 生成一个 DraftContent，并提交给 Bridge。
4. Worker 校验 DraftContent、构建 ArtifactEnvelope、应用 EvidenceGate，并停在准确的审批或继续执行确定性动作。
5. Gate 等待由如流/Comate 现有审批适配器处理；审批被批准后，live Stop hook 消费 hash-bound handoff。宿主重启后由用户调用“继续”或从卡片进入。
6. iPipe watcher 只报告远端变化，不代替 worker 摄取证据或推进阶段；后续 drive 仍检查匹配的 build、revision 和环境证据。

该流程借鉴 Temporal 的 Workflow/Activity/Signal 职责分离：状态推进可从保存记录继续；外部副作用留在既有幂等 controller；人的批准是绑定输入的信号。首期继续使用当前本地 SQLite 与 receipt/reconciliation，不部署 Temporal 服务。

## 5. 错误与恢复

- `PROJECT_NOT_READY`：显示缺失的 profile/能力并指向一次性 setup，不创建半初始化运行。
- `APPROVAL_WAIT`：明确展示审批内容摘要、仓库/模块、分支、revision、批准后动作；不把等待描述成 worker 故障。
- `PRODUCER_WAIT`：提供 job 的固定输入、schema 和适用 skill；不让模型重造运行上下文。
- `WORKER_LEASE_HELD`：显示已有 worker 正在驱动；不启动第二个 worker。
- `WORKFLOW_SPEC_DRIFT`、`PROFILE_CONFLICT`、revision/hash mismatch、损坏 artifact、未知外部写结果：安全停止，显示维护动作和需要核对的证据，不自动重发。
- 可修正的草案校验失败：复用同一个仍有效 ProducerJob 并返回精确错误；审批后的内容变化产生新的审批 hash。
- watcher、Stop hook 或 AgentBridge 失败不得静默报告成功；保留 durable checkpoint 并展示恢复入口。

## 6. 文档与 Skill 结构

- `tom-autodev/SKILL.md` 只保留入口意图路由、AgentBridge 调用契约、阶段 producer 边界及关键安全不变量；将恢复参数与平台细节链接到 references。
- README 的“日常使用”改为少量 `/tom-autodev` 示例、运行卡说明和继续方式；首次项目注册、安装和平台 watcher 仍单列一次性准备。
- `docs/OPERATIONS.md` 成为 CLI、Bridge、恢复与 watcher 的权威维护参考，并明确 `resume`（检查点读取）和 `continue`（worker 驱动）的区别。
- LoopX 的目标/状态摘要作为用户交互参考，不在本次加入 Dashboard、Goal 系统或新的状态源。

## 7. 范围外

- 改变审批数量、G0–G10 语义或把人工 gate 改成自动批准。
- 新建后台 LLM、替换 Comate、自动推断项目/卡片归属、静默选择多个 run。
- 自动安装或启用 launchd/cron watcher；若后续需要一键安装，应单独设计可预览、无覆盖的本机配置变更。
- 新建浏览器 UI、接入 Temporal Server、迁移现有数据库或更改业务项目 profile。
- 业务仓编译、测试、iCode 提交、iPipe 触发、重跑、发布或合并策略变更。

## 8. 验收标准

1. 新需求可从 `/tom-autodev` 进入，使用者提供明确卡片和项目；重复启动遵守现有幂等性，歧义时请求选择。
2. AgentBridge 使用现有 WorkerDriver 推进，直到产生 ProducerJob、ApprovalJob、等待、终态或具体阻塞；模型不负责内部状态迁移。
3. Agent 只提交 DraftContent；Bridge/worker 构造并校验 envelope。当前 CLI 没有 `submit-draft` 的矛盾被消除，并有可调用的实际接口。
4. G0–G10、hash-bound approval、EvidenceGate、run/workspace 锁和副作用幂等行为不降级；重启、重复调用和多 run 不串单、不重复提交或重跑。
5. 批准 handoff 可在仍存活的 Comate 会话中续跑；已结束会话有明确“继续”入口。iPipe 结果继续由远端证据确认。
6. 普通用户能从摘要知道谁该做什么；维护者能按高级文档恢复，不依赖模型猜内部命令。
7. 更新后的 README、SKILL 和 OPERATIONS 对 start/status/resume/continue/submit-draft 的职责一致，所有命令示例与实际 CLI 接口一致。

## 9. 回滚

Bridge 是无状态装配层，回滚其入口和文档即可恢复既有 Python/CLI 接口。保留现有 SQLite schema、event payload、approval ledger 和 artifact 格式，避免需要数据迁移。若桥接接口验证失败，恢复对应入口文件，既有运行记录仍可由原有只读和维护接口访问。
