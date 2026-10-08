# Producer 阻塞结果入口

`IMPLEMENT` ProducerJob 在没有真实 Change Set 时，必须使用
`report-producer-block` 报告 `PLAN_INCOMPLETE`，不能提交伪造的
`change-set`，也不能直接写运行事件或数据库。

入口从当前 run/job/action 读取绑定，报告必须固定：

- `run_id`、`job_id`、`action_id`、`source_event_id`、`task_id`、`input_hash`
- 已批准 PLAN 的 artifact、content hash、G4 approval 和 approval input hash
- `source_revisions`、阻塞 `reason` 和 `evidence`
- `workspace_state: NO_CODE_CHANGES`

CLI 示例：

```bash
python3 tom-autodev/scripts/cli.py report-producer-block \
  RUN_ID producer:ACTION_ID producer-block.json
```

该命令只登记不可变的 `producer-block-report` 和待消费的控制面结果。
下一次正规 `drive`/`continue` 由 `WorkerDriver` 验证并执行
`IMPLEMENT -> DIAGNOSE`，诊断仍须 G6；`DIAGNOSE -> PLAN` 仍须 G4。
报告不会生成 IMPLEMENT artifact，也不会声称发生了编译、测试或 iPipe
执行。当前没有 IMPLEMENT artifact 时，诊断 predecessor 明确绑定阻塞报告
与 approved PLAN，禁止猜测历史 Change Set。

若阻塞实际改变 Spec/GRILL 验收，诊断应选择上游路线并重新取得相应审批。
用户需求取消 MSS 全局严格等值属于 `USER_REQUIREMENT_CHANGED`；不能改写成
`CODE_FAILURE`/`PLAN_INCOMPLETE`，也不能把“svc 缺失”当作事实。该分类的诊断只能
`route=GRILL`（或 `STOPPED`），控制面拒绝改分类或路由到 SPEC/PLAN；G6 批准后修复方向
作为 `revision_context` 进入 GRILL，重新取得 G1 及其后各门。

BGW-1995 的教训：10-05 用户取消严格等值，阻塞报告记为 `CONTRACT_INPUT_MISSING`，诊断
路由 PLAN；PLAN 只服从仍写着 B2（严格等值）的决策日志和 Spec，于是每次重出计划都恢复了
严格等值，10-08 路由 SPEC 的诊断也不再提这条变更。G5 “需修改”现在原地重做 IMPLEMENT，
重做的 producer 可以按上面的方式上报；此时 `NO_CODE_CHANGES` 指本 job 未改代码，被驳回候选
的提交须列入 `evidence`，留在工作区由 GRILL 之后的重做修订或回退。残余风险必须写入诊断和后续
计划，不能宣称 100% 无 bug。
