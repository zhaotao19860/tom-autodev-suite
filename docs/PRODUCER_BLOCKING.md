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
`CODE_FAILURE`，也不能把“svc 缺失”当作事实。残余风险必须写入诊断和后续
计划，不能宣称 100% 无 bug。
