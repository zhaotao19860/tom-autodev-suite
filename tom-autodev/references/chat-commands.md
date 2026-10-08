# Comate `/tom-autodev` 命令

`/tom-autodev CARD ACTION` 是 Comate skill 的自然语言入口，不是
`cli.py` 的字面命令。Comate 先确认 card 对应的项目和唯一 run，再调用下面
的控制器动作；不会通过目录名猜项目，也不会在多个 run 之间按时间自动选择。

## 支持的动作

| Comate 命令 | 控制器映射 | 语义 |
|---|---|---|
| `/tom-autodev CARD 开始` | `cli.py process CARD PROJECT` | 无历史时创建并驱动；有唯一活动 run 时继续驱动；遇到终态返回 `RUN_ALREADY_TERMINAL`，不自动重启 |
| `/tom-autodev CARD 结束` | `cli.py stop CARD_OR_RUN` | 停止唯一活动 run；保留事件、审批、产物和工作区审计，不物理删除 |
| `/tom-autodev CARD 状态` | `cli.py status CARD_OR_RUN` | 读取紧凑进度摘要；无活动 run 时可查看唯一终态 run；多个候选时列出候选 |
| `/tom-autodev CARD 当成状态` | `cli.py status CARD_OR_RUN` | “当成状态”按状态查询处理，等同于“状态” |
| `/tom-autodev CARD 继续` | `cli.py continue CARD_OR_RUN` | 消费有效 handoff，或从持久化检查点继续驱动唯一活动 run |
| `/tom-autodev CARD 需修改 <意见>` | `cli.py revise RUN --approval-id <当前门> --feedback <意见> --actor <你>` → `continue` | 对当前待审批/已选需修改的门直接提意见并原地重做，一步代替“如流点需修改＋回复意见” |
| `/tom-autodev CARD 重新开始` | `stop`（如有活动 run）→ `cli.py start --fresh CARD PROJECT` → `drive` | 显式创建新的审计 run；旧 run 不删除，新的 run 驱动到下一个动作 |

## 与 CLI 的区别

- “开始”对应日常入口 `process`，不是裸 `start`。只有用户明确要求“只创建
  run、暂不驱动”时才调用 `start`。
- “继续”对应 `continue`，不是只读的 `resume`。`resume` 只查看检查点和未确认
  intent，不执行阶段。
- “结束”对应 `stop`，不是删除。控制面保留审计记录，避免审批、外部 intent 和
  产物失去可追溯性。
- “状态”允许查询唯一终态历史；`结束`、`继续`、`开始`等动作只允许绑定唯一活动
  run。多个候选时必须展示 run id、状态和项目，让用户明确选择。

## 项目参数

自然语言命令可以省略 `PROJECT`，但只有在以下情况之一成立时才可继续：

1. card 已绑定唯一活动 run，项目从该 run 的不可变 intake 读取；
2. card 只有一个可用的需求级 profile，且用户已确认该项目；
3. 用户在当前对话中明确指定了项目。

否则先返回项目候选并要求确认。不能因为 `BGW-` 前缀、目录名或最近一次使用
的项目就静默绑定 `bgw`。

## 典型用法

```text
/tom-autodev BGW-1995 开始
/tom-autodev BGW-1995 状态
/tom-autodev BGW-1995 继续
/tom-autodev BGW-1995 需修改 去掉 c2s_mss 严格等值，覆盖 NAT44/NAT64/NAT66
/tom-autodev BGW-1995 结束
/tom-autodev BGW-1995 重新开始
```

`需修改 <意见>` 会定位该 run 当前唯一可修改的门（G1–G6），把意见作为该门的
“需修改”决策记在 comate 渠道并原地重做本阶段，再 `continue` 投递新版审批卡；
无需先在如流点按钮。门已同意或已超时则不能再改，会提示重新发起。

`开始`、`继续`、`重新开始`返回 `PARKED / APPROVAL_WAIT` 时，只创建或投递审批，
不会替用户批准；`状态`和进度通知也不会伪装成审批卡。

当“继续”发现如流审批投递结果未知时，不能重复执行“继续”期待它自动重发。
状态会列出 `approval_id` 和 `intent_id`，并提示结构化确认：
“未收到，允许重发”。确认后控制面只关闭指定的未知 intent 并沿原
`approval_id` 链路重发，不创建新的 `G1#retry-N` 审批，不改变 `input_hash`；
没有确认时运行保持 `RECOVERY_REQUIRED`，避免重复发送。
