# 操作参考

日常入口见 [README](../README.md)。本页面向配置宿主、维护控制器或恢复已有 run 的使用者。除标明的 Python 接入示意外，所有终端命令都从**仓库根目录**执行。

## 三组容易混淆的接口

| 接口 | 实际作用 |
|---|---|
| `cli.py start REQUIREMENT PROJECT` | 读取 iCafe 快照，创建 run；不启动后台 worker/模型 |
| `cli.py next RUN` | 查询当前动作和输入要求 |
| `cli.py resume RUN` | 返回恢复检查点、结果不明的外部 intent；不执行阶段 |
| Python `worker_driver.advance(...)` | 推进已满足条件的确定性工作，返回草案、审批、平台等待或阻断原因 |
| Python `worker_driver.resume(...)` | 消费该 run 已结算的审批 handoff，再调用 worker 推进 |
| Python `worker_driver.submit_draft(...)` | 提交一个 ProducerJob 的内容，由 worker 校验、封装、保存并按策略完成阶段 |
| `cli.py advance RUN STATE ...` | 维护用的单次状态迁移入口，**不是** worker 循环 |
| `cli.py complete-phase RUN ENVELOPE` | 维护用的产物摄取入口，**不是**子 skill 的草案接口 |

当前 CLI 没有 `worker` 或 `submit-draft` 子命令。不要把 Python 函数名直接拼成终端命令，也不要让模型自行补写状态库来替代缺失的宿主接入。

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

IPIPE/RELEASE 需要真实 `ipipe_api`，缺少 adapter 时会返回等待/阻断；不会凭空运行。并发驱动必须注入 `LockManager`：`locks=None` 的调用不启用 run 级租约。不同 run 仍可能竞争同一个仓库资源，应保留项目定义的 workspace 锁和所有权检查。

同一 run 的审批输入绑定到当时的 workflow/profile/代码版本。`WORKFLOW_SPEC_DRIFT` 说明当前代码中的 workflow 定义已变化，需还原兼容版本或做显式迁移；不要删除保存的指纹绕过检查。

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
| G6 | 诊断和修复方向；当前无 diff 提案的限制见整合报告 |
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
python3 tom-autodev/scripts/cli.py resume "$RUN_ID"
python3 tom-autodev/scripts/cli.py next "$RUN_ID"
```

外部操作状态不明时，先查询平台并与持久化 intent/回执核对；不要用再次提交“试一下”。IMPLEMENT 恢复时检查实际文件与候选 diff，空 checklist 不意味着之前的编辑没发生。

只有符合对应恢复场景时才使用专门命令：

```bash
python3 tom-autodev/scripts/cli.py recover-rebuilt-change-set --help
python3 tom-autodev/scripts/cli.py recover-stale-submit --help
python3 tom-autodev/scripts/cli.py recover-stale-rebuilt-plan --help
python3 tom-autodev/scripts/cli.py recover-legacy-pipeline-plan --help
python3 tom-autodev/scripts/cli.py repin-profile --help
python3 tom-autodev/scripts/cli.py abandon-intent --help
```

`repin-profile` 需要保存的上一版 profile 副本和对应审批；直接编辑配置不会自动改变已运行任务的绑定。`recover-legacy-pipeline-plan` 仅用于升级前已进入 IPIPE、事件里没有 `pipeline_plan`（RELEASE 报 `PIPELINE_PLAN_REQUIRED`）的历史 run：它从该 run 自己不可变的、获审的提交与固定 profile 确定性重建并回填冻结计划，不提交、不发布、不审批、不调用任何 runtime，幂等；在途旧构建会按模块重跑，不会被当作发布证据。`abandon-intent` 写入带原因和操作人的放弃记录，不把未知结果当成功。不要手改数据库或删除记录来解除阻断。

默认状态库为 `~/.tom-autodev/state.sqlite`。自定义根目录参数放在子命令前，后续命令须保持一致：

```bash
python3 tom-autodev/scripts/cli.py --config-root /path/to/autodev-state status "$RUN_ID"
```

## 产物与知识保存

恢复和校验以本地归档产物为准，KU/iCafe 用于协作和知识检索。当前发布范围如下：

| 阶段 | 协作侧写入 |
|---|---|
| INTAKE、SPEC、RELEASE | KU 与 iCafe |
| REVIEW、DIAGNOSE | KU |
| IPIPE | iCafe 里程碑 |
| GRILL、TASKS、PLAN、IMPLEMENT | 保留本地产物，不逐阶段写 KU/iCafe |

读取归档使用 `cli.py artifact show ARTIFACT_ID` 的完整性校验入口。需要查看全部事件可用 `status RUN --json`；CLI 没有 `trace` 子命令。

## 终态后优化

G10 基于已归档运行生成总结和提案，经批准后才应用；允许目标限定在控制面目录，拒绝修改业务仓、profile、流水线或密钥。先查看精确参数：

```bash
python3 tom-autodev/scripts/cli.py optimize --help
```

发生过一次的现象应先核对版本、触发条件和反例；只有适用的结论才进入可复用知识。机械故障优先补脚本校验和回归，模型决策变化使用 [行为场景](../evals/README.md) 比较。
