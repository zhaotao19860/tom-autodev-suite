# 操作参考

日常入口见 [README](../README.md)。本页面向配置宿主、维护控制器或恢复已有 run 的使用者。除标明的 Python 接入示意外，所有终端命令都从**仓库根目录**执行。

## 只读预检与后台值班

```bash
python3 tom-autodev/scripts/cli.py preflight bgw
```

预检结果的 `automation` 字段会检查 `~/.comate/hooks.json` 和
`~/.comate/hooks.local.json` 中是否引用 `ide_turn_hook.py`，并确认 watcher 命令文件存在。
cron、launchd 或其他外部调度器没有统一的心跳接口，因此审批 watcher 和 iPipe watcher 的调度配置、运行状态和最近成功时间会显示为 `unknown`；预检只读，不会安装、启动或唤醒它们。Stop-hook 依赖仍存活的 Comate 会话，不能唤醒已经结束的会话。

## 日常接口与维护接口

Comate 中的 `/tom-autodev CARD ACTION` 是这些 CLI 操作的自然语言入口。完整映射、
项目解析和“重新开始”规则见
[`tom-autodev/references/chat-commands.md`](../tom-autodev/references/chat-commands.md)。

| 命令 | 作用 |
|---|---|
| `cli.py process CARD PROJECT` | 推荐入口：无历史时 start + drive，唯一活动 run 用 continue；多候选拒绝选择，项目不匹配拒绝推进，终态返回 RUN_ALREADY_TERMINAL |
| `cli.py start CARD PROJECT` | 读取 iCafe 快照并创建 run；不启动后台模型 |
| `cli.py start --fresh CARD PROJECT` | 旧 run 已停止后创建新的审计 run；不删除旧事件、审批或产物 |
| `cli.py drive TARGET` | 由 WorkerDriver 驱动到 ProducerJob、审批、远端等待、终态或阻塞；TARGET 可为 run id 或唯一活动 run 的卡号 |
| `cli.py status TARGET` | 输出紧凑进度摘要；按卡号优先选唯一活动 run，无活动 run 时回落到唯一终态 run |
| `cli.py status TARGET --json` | 输出完整事件 JSON；多个活动 run，或无活动 run 时存在多个终态历史，会返回候选 |
| `cli.py continue TARGET` | 消费有效审批 handoff，或从持久化检查点继续驱动；卡号只匹配活动 run |
| `cli.py submit-draft TARGET JOB_ID DRAFT_JSON` | 提交当前 ProducerJob 的 DraftContent 并自动发起所需审批；文件不能包含 ArtifactEnvelope |
| `cli.py context TARGET` | 只读查询当前 action 的固定输入引用、版本、schema 和当前 ProducerJob（含已保存草案状态）；不执行阶段 |
| `cli.py stop TARGET` | 对唯一活动 run 记录停止请求，保留事件、产物、审批和工作区 |
| `cli.py resume RUN` | 只读检查点和未确认外部 intent；不执行阶段 |
| `cli.py next RUN` | 维护/诊断用的只读动作查询；日常流程不要手动按它编排 |
| `cli.py advance RUN STATE ...` | 维护用的单次状态迁移入口；不能替代 worker |
| `cli.py complete-phase RUN ENVELOPE` | 维护用的产物摄取入口；不是 ProducerJob 提交接口 |

普通流程使用 `process`、`status`、`context`、`continue`、`submit-draft` 和 `stop`。需要分步创建时仍可用 `start` + `drive`。`resume` 仍保留原来的只读语义；真实续跑用 `continue`。Producer 只返回 DraftContent，worker 负责 envelope、schema/hash 校验、审批绑定、证据门和外部副作用。审批未送达或输入不一致时，命令返回明确错误，不报告成功。

协作群的 topic/name 只从启动时冻结的 iCafe requirement snapshot 派生。旧 profile
中的 `approval_channels.group_topic` 不会覆盖当前需求；如果历史 run 已记录了不一致的群绑定，
`drive`/`continue` 会 fail-closed，不会重试建群或复用旧 intent。保留旧 run 的事件、审批和
intent 后，使用 `stop RUN`，再用 `start --fresh CARD PROJECT` 创建新的审计 run。

建群成功回执只要求平台返回有效 `group_id`。如流创建接口不保证回显 `bot_id`，网关会使用
已放入 `robotList` 的机器人身份补齐本地回执。网关按 `infoflow.group.create:<run_id>` 将脱敏
回执写入独立 journal；`continue` 在 phase recovery 门禁前先查询该 journal，并且只补写原
intent 的 receipt，绝不重新创建群。旧版本网关没有这份 journal 时，状态只能保持
`QUERY_REQUIRED`；不能按群名猜测或直接重发。应取得明确的 group ID 后再走受控核对/认领。
历史 intent 已确认对应某个群时，可使用：

```bash
python3 tom-autodev/scripts/cli.py adopt-group RUN_ID GROUP_ID --actor USER
```

该命令通过网关按明确 ID 查询群信息和成员接口，名称不匹配或成员查询失败都会拒绝，
成功后只补写原 intent 的 receipt。

`submit-draft` 自动将 worker 返回的 gate 与精确 `approval_input_hash` 交给审批投递路径；投递成功返回 `PARKED / APPROVAL_WAIT`、`draft_saved: true` 和审批记录，等待人工决定。投递失败返回非零退出码并保留草案；用 `continue TARGET` 复用草案重试，勿重新生成内容。Python worker/bridge API 仍将 `APPROVAL_REQUIRED` 交给宿主处理。

`context` 不推进阶段、恢复 SUBMIT、写 action 缓存或创建 ProducerJob，只显示当前 action 对应的任务，不列历史任务或草案正文。输入产物的正文通过 `artifact show ARTIFACT_ID` 校验读取。任务的 `mode: merged` 表示 SPEC 草案需同时包含 `{spec, dag}`。`status` 在终态隐藏旧的待审批和未确认操作提示；历史原始事件仍可通过 `status TARGET --json` 查询。

## Worker 接入

宿主负责装配 `Orchestrator`、知识同步、iCode/iPipe adapter 和锁管理器，并处理 INTAKE 的审批与协作初始化。下面是**已完成装配后的调用示意**，不是独立服务启动脚本：

```python
import worker_driver

result = worker_driver.advance(
    orch,
    run_id,
    knowledge_sync=knowledge_sync,
    icode_runtime=icode_runtime,
    icode_skill=icode_skill_path,
    ipipe_api=ipipe_api,
    locks=locks,
    owner_token=worker_owner,
)
```

按返回值安排下一步，而不是不加判断地循环：

1. `PRODUCER_WAIT`：从返回的 `producer_job` 读取 ID、phase、mode、result schema 和固定输入；让当前 Comate Agent 按对应 skill 生成内容。
2. 将内容交给 `worker_driver.submit_draft(orch, run_id, job_id, draft, knowledge_sync=knowledge_sync)`。普通阶段只交 schema 对应的 content；merged SPEC 交 `{spec, dag}`。IMPLEMENT 在自有 worktree 生成实际代码和提交，再提交候选内容。
3. 遇到 `APPROVAL_REQUIRED` 或 `APPROVAL_WAIT`，由宿主展示对应 gate 和准确的 `approval_input_hash`。审批结算后再次调用 `advance`，或消费 handoff 的 `resume`。
4. 已保存、已获批的草案由 worker 继续消费，不再唤醒模型重写。`DRAFT_SCHEMA_INVALID` 等可重试错误按返回信息修正草案；输入漂移、外部结果不明或能力缺失先处理阻断原因。
5. 非终态的 iPipe/发布等待应按平台状态安排下一次查询；看到 `TERMINAL` 仍需读取具体状态，`STOPPED` 不代表交付成功。

IPIPE/RELEASE 需要真实 `ipipe_api`，缺少 adapter 时会返回等待/阻断；不会凭空运行。并发驱动必须注入 `LockManager`：run 级租约串行化 WorkerDriver 的 `advance` 调用；`locks=None` 不启用该租约。`submit_draft` 不持有这把租约，依靠 ProducerJob 的事务/幂等校验及阶段、副作用的幂等键处理重复提交；这不构成覆盖所有 run 写操作的全局单写者保证。不同 run 仍可能竞争同一个仓库资源，应保留项目定义的 workspace 锁和所有权检查。

同一 run 的审批输入绑定到当时的 workflow/profile/代码版本。`WORKFLOW_SPEC_DRIFT` 说明当前代码中的 workflow 定义已变化，需还原兼容版本或做显式迁移；不要删除保存的指纹绕过检查。

`IpipeRuntime` 即使由宿主缓存，也会在新增操作或证据写入前核对当前 profile 文件与最新批准的 pin。发生 `PROFILE_CONFLICT` 后先核对配置；合法 re-pin 后通过 `Orchestrator.ipipe_runtime` 重新构造 runtime。已完成 trigger/rerun 的匹配回执仍可只读回放，结果不明的 intent 不能借回放名义继续产生写入。

`Orchestrator.icode_runtime` 同样要求完整的项目 profile，提交策略从批准的配置读取。`submit_to_ipipe` 在调用提交 adapter 前检查配置；缓存的 `IcodeRuntime.submit` 在预检、远端查询和提交/回执边界再次核对。配置重新批准后应重建对象；若推送已发出才发现漂移，保留 pending intent，先恢复兼容配置并查询已有 CR，不能直接重发。已经完成的 controller 提交回执可只读重放；runtime 的 submit 会执行预检，不属于纯回放入口。

## 审批编号

编号表示审批种类，不是一笔批准覆盖整个运行。以当前 action 的具体内容与输入指纹为准。

| Gate | 审批对象 |
|---|---|
| G0 | 需求卡、项目、变更类别与协作绑定 |
| G1 | 澄清结果或架构决策 |
| G2 | Spec、测试接口和环境要求；merged 模式连同 DAG |
| G3 | 独立 TASKS 阶段的 DAG；merged 模式免去这道门 |
| G4 | 工作区绑定，以及需要审批的单任务计划；两者是不同动作 |
| G5 | 实际业务与产品测试候选改动 |
| G6 | 诊断和修复方向；无补丁提案可用 `repair_diff_hash=null`，后续真实候选仍需 G5 |
| G7 | iCode 提交及初始 iPipe 触发，各自绑定准确的动作输入 |
| G8 | 失败阶段重跑或人工阶段继续 |
| G9 | 发布相关动作和版本证据；worker 校验已发布的锁定 build |
| G10 | 终态后对控制面自身的优化提案 |

Comate 和如流共享 `approval_id`，首个有效回应生效。审批卡应列出仓库/模块、目标分支、revision 和批准后的动作；跨仓逐仓列出，G7 区分新 CR 与追加 revision。

通常直接回复审批卡即可。维护时可查看以下入口的精确参数：

```bash
python3 tom-autodev/scripts/cli.py request-approval --help
python3 tom-autodev/scripts/cli.py approve --help
python3 tom-autodev/scripts/cli.py await-approval --help
python3 tom-autodev/scripts/cli.py reissue-approval --help
```

重新生成内容、换版本或改参数后，要审批新的输入。非 gate 问答的回答不能替代一个绑定 hash 的批准。

## 如流与 Comate 续跑

如流网关需要 Node.js、配置好的机器人和审批成员。`watch-approvals` 消费回应、更新审批台账并产生 handoff；它本身不完成模型阶段。

```bash
# 单次处理；可能投递通知，需使用已配置的运行环境
python3 tom-autodev/scripts/cli.py watch-approvals --once
python3 tom-autodev/scripts/cli.py watch-ipipe --once
```

在 Comate 的 `Stop` hook 中注册 [ide_turn_hook.py](../tom-autodev/scripts/ide_turn_hook.py)，可让仍存活的 Agent 会话收到已批准动作的继续上下文。该 hook 不会自己执行阶段，也不能把一个已结束的会话重新启动。它使用默认 `~/.tom-autodev` 状态目录；使用自定义配置根时，必须同步核对宿主集成方式。

hook 的注册位置为 `~/.comate/hooks.json` 或 `~/.comate/hooks.local.json`，具体配置遵循当前 Comate 的 hook 格式。审批超时、未收到消息和授权规则见 [审批策略](../tom-autodev/references/approval-policy.md)。

## iPipe 和平台 Review

运行失败或人工阶段需要继续时，先读取项目 skill 中的拓扑、产品与参数映射，再由控制器计算参数和 G8 输入指纹。

```bash
python3 tom-autodev/scripts/cli.py ipipe-rerun --help
python3 tom-autodev/scripts/cli.py ipipe-adopt --help
python3 tom-autodev/scripts/cli.py ai-review --help
```

`ipipe-rerun` 的 `--parameter` 接受**参数名**，值由本次运行的产品/提交证据推导；不是任意 `NAME=VALUE`。例如 BGW 的 `get_bgw_test_case`、`get_bgwagent` 应先核对 [参数映射](../tom-project-bgw/references/runtime-topology.md)。`--print-parameters` 只展示脱敏后的参数，`--print-input-hash` 返回本次 G8 绑定值；实际重跑仍需匹配的批准。

`ipipe-adopt` 用于绑定平台已经触发、确属本次 CR 的 build。平台 AI Review 在 CR 已存在时启动，返回的 `conversation_id` 用于后续 poll；平台建议仍须逐条验证，不能直接作为修改指令。

多模块项目按每个必需模块的 submission、revision、环境和 build 校验证据。全部必需模块验证通过后进入发布确认；发布也检查每个必需模块，任一尚未发布就返回 `RELEASE_WAITING`。

## 中断与配置变更

恢复先看状态，保留现有工作区：

```bash
RUN_ID='替换为已有 run_id'
python3 tom-autodev/scripts/cli.py status "$RUN_ID"
python3 tom-autodev/scripts/cli.py resume "$RUN_ID"       # 只读检查点
python3 tom-autodev/scripts/cli.py continue "$RUN_ID"    # worker 续跑
```

外部操作状态不明时，先查询平台并与持久化 intent/回执核对；不要用再次提交“试一下”。IMPLEMENT 恢复时检查实际文件与候选 diff，空 checklist 不意味着之前的编辑没发生。
协作群的 `infoflow.group.create` 也遵守这一规则：`resume` 只读列出待查询项，`continue`
才会先执行网关回执的只读核对；核对不到时继续停在 `RECOVERY_REQUIRED`，不会调用 create。

KU 文档和目录操作统一使用 `/ku-doc-manage` skill 的 `bin/ku` 入口。默认会从当前
Comate skill 环境发现入口，也可显式指定：

```bash
export BAIDU_CC_USERNAME=zhaotao02
export TOM_AUTODEV_KU_SKILL_DIR=/Users/tom/.comate/skills/.system/ku-doc-manage
```

skill 产生的前置 debug 文本不会再被误判为 JSON 损坏；如果入口、身份或远端查询确实
不可用，控制面会保留对应的具体 CLI 错误。

只有符合对应恢复场景时才使用专门命令：

```bash
python3 tom-autodev/scripts/cli.py recover-rebuilt-change-set --help
python3 tom-autodev/scripts/cli.py recover-stale-submit --help
python3 tom-autodev/scripts/cli.py recover-stale-rebuilt-plan --help
python3 tom-autodev/scripts/cli.py recover-legacy-pipeline-plan --help
python3 tom-autodev/scripts/cli.py repin-profile --help
python3 tom-autodev/scripts/cli.py abandon-intent --help
python3 tom-autodev/scripts/cli.py confirm-approval-redelivery --help
python3 tom-autodev/scripts/cli.py migrate-workflow-spec --help
python3 tom-autodev/scripts/cli.py revise --help
python3 tom-autodev/scripts/cli.py rewind --help
```

`confirm-approval-redelivery` 只用于如流投递结果未知的那一条 intent：`status` 会给出
`approval_id` 和 `intent_id`，`--confirm` 必须精确填写 `未收到，允许重发`，并带上核对依据
和操作人。它作废原 intent、链接一个新的投递 key，重发同一个审批，不创建新审批、不改
`input_hash`。

```bash
python3 tom-autodev/scripts/cli.py confirm-approval-redelivery "$RUN_ID" APPROVAL_ID INPUT_HASH INTENT_ID \
  --confirm '未收到，允许重发' --reason '已在如流确认未收到该卡片' --actor zhaotao02@baidu.com
```

`WORKFLOW_SPEC_DRIFT` 表示该 run 固定的 workflow policy 与当前版本不一致。此时除迁移门
本身外的写入都会被拒绝：先 `--request` 开 `WORKFLOW_SPEC_MIGRATION` 门，由人批准后再用
精确的 `approval_id` 应用迁移；迁移只移动策略 pin，不重写阶段事件、不改既有审批绑定。
只有迁移获批并应用之后，其它门的未知投递才可以走上面的受控重发。

如流已经回复并落账了精确的 `WORKFLOW_SPEC_MIGRATION` 批准，`continue` 会消费当前
run、当前 workflow_spec_migration.plan 生成的相同 `input_hash`，调用现有迁移 apply，
然后再交给 worker 恢复。它不会自动批准、重新开门、重开审批或直接修改 pin；未批准、
驳回、旧 hash、跨 run 的审批仍保持 `WORKFLOW_SPEC_DRIFT`。`status` 只读展示“迁移已批准
待应用”，并提示调用 `continue`，不会在查看状态时执行迁移。

```bash
python3 tom-autodev/scripts/cli.py migrate-workflow-spec "$RUN_ID" --request
python3 tom-autodev/scripts/cli.py migrate-workflow-spec "$RUN_ID" --approval-id APPROVAL_ID
```

### 改着改着发现前面要动：revise 与 rewind

不需要新建 run。两条路径都只追加事件，原有事件、产物、审批记录全部保留；重做出的每一版
仍要过各自的门。

- **需修改后按意见重做**：G1/G2/G3/G4 选择“需修改”时，`status` 显示“已驳回待修改”。
  把修改意见交给 Comate（或在如流回复 `需修改 <approval_id> 修改意见`），它以这条驳回为
  授权，带着意见和上一版草案重新进入同一阶段；输入“继续”会自动触发重做，不需要
  再手动执行命令。新草案的审批消息附“本次修改说明”和“与上一版的差异”。
  旧审批只对当前等待中的那一版草案有效。

```bash
python3 tom-autodev/scripts/cli.py revise "$RUN_ID" --approval-id REJECTED_ID \
  --feedback '验收场景缺少回滚' --actor zhaotao02@baidu.com
```

- **变更请求回退（rewind）**：需要回到更早阶段时，开一道 `CHANGE_REQUEST` 审批，绑定
  当前位置、目标阶段、原因，以及可选的 profile 改动。允许的回退：SPEC→GRILL/SPEC；
  TASKS→GRILL/SPEC/TASKS；WORKSPACE 与 ARCHITECTURE_REVIEW→GRILL/SPEC/TASKS。代码阶段
  之后的返工仍走 DIAGNOSE。ARCHITECTURE_REVIEW 只能通过 rewind 离开。
  `test_repo` / `business_repos` 只能随 rewind 一起改，且仅限该 run 从未绑定过工作区；
  先把被钉住的 profile 复制一份，再编辑原文件，用 `--previous-profile` 指向副本。
  批准只在运行仍停在发起时的位置才有效；运行动过就返回 `RUN_MOVED`，需按新位置重新发起。

```bash
cp ~/.tom-autodev/config/projects/bgw/BGW-1995.yaml /tmp/BGW-1995.pinned.yaml
# 编辑原 profile 的 test_repo.branch / lock
python3 tom-autodev/scripts/cli.py rewind "$RUN_ID" --to SPEC --reason '测试仓库改用新分支' \
  --actor zhaotao02@baidu.com --previous-profile /tmp/BGW-1995.pinned.yaml          # 预览
python3 tom-autodev/scripts/cli.py rewind "$RUN_ID" --to SPEC --reason '测试仓库改用新分支' \
  --actor zhaotao02@baidu.com --previous-profile /tmp/BGW-1995.pinned.yaml --request  # 开门
python3 tom-autodev/scripts/cli.py rewind "$RUN_ID" --approval-id APPROVAL_ID          # 批准后执行
```

回退或重做之后，回退前开出的旧阶段审批卡视为已被取代：不再催办、不再超时重发
（`APPROVAL_SUPERSEDED`），也不再出现在 `status` 的待审批列表里。重发次数用完
（`APPROVAL_RETRY_EXHAUSTED`）的设计阶段门，可用同阶段 rewind 换一道新门。

`repin-profile` 需要保存的上一版 profile 副本和对应审批；直接编辑配置不会自动改变已运行任务的绑定。`recover-legacy-pipeline-plan` 仅用于升级前已进入 IPIPE、事件里没有 `pipeline_plan`（RELEASE 报 `PIPELINE_PLAN_REQUIRED`）的历史 run：它从该 run 自己不可变的、获审的提交与固定 profile 确定性重建并回填冻结计划，不提交、不发布、不审批、不调用任何 runtime，幂等；在途旧构建会按模块重跑，不会被当作发布证据。`abandon-intent` 写入带原因和操作人的放弃记录，不把未知结果当成功。不要手改数据库或删除记录来解除阻断。

默认状态库为 `~/.tom-autodev/state.sqlite`。自定义根目录参数放在子命令前，后续命令须保持一致：

```bash
python3 tom-autodev/scripts/cli.py --config-root /path/to/autodev-state status "$RUN_ID"
```

## 产物与知识保存

恢复和校验以本地归档产物为准，KU/iCafe 用于协作和知识检索。当前发布范围如下：

| 阶段 | 协作侧写入 |
|---|---|
| INTAKE、RELEASE | KU 与 iCafe |
| SPEC | KU |
| IPIPE | iCafe 里程碑 |
| GRILL、TASKS、PLAN、IMPLEMENT、REVIEW、DIAGNOSE | 保留本地产物，不逐阶段写 KU/iCafe |

读取归档使用 `cli.py artifact show ARTIFACT_ID` 的完整性校验入口。需要查看全部事件可用 `status RUN --json`；CLI 没有 `trace` 子命令。

## 终态后优化

G10 基于已归档运行生成总结和提案，经批准后才应用；允许目标限定在控制面目录，拒绝修改业务仓、profile、流水线或密钥。先查看精确参数：

```bash
python3 tom-autodev/scripts/cli.py optimize --help
```

发生过一次的现象应先核对版本、触发条件和反例；只有适用的结论才进入可复用知识。机械故障优先补脚本校验和回归，模型决策变化使用 [行为场景](../evals/README.md) 比较。
