# GAP-WF08：配置漂移入口矩阵的后续任务

本文件承接 `TAD-REVIEW-EXIT / 1.0` 首次工程验收中的必要证据缺口，不另开一次全仓评审，也不宣布发现新的 P1。第二轮结束后仍不能用 iCode/iPipe 的通过结果代表所有可写入口。

状态：**待取证，影响完整 C 结论**。负责人：仓库维护者 Tom；执行可交由其指定实现者。没有风险接受记录；关闭期限为下一次声明 C PASS 之前。本次不自动启动第三轮。

## 已有证据与待补范围

| 入口组 | 已有证据 | 尚需回答的问题 |
|---|---|---|
| iCode factory / controller submit / cached runtime / 兼容客户端 | [15 项专项回归](../../tom-autodev/scripts/tests/test_icode_profile_guard.py)，含 SQLite 不变、无推送、未知 intent、repin、正常提交、纯 controller 回执 | 当前确认触发已覆盖；作为后续回归，不重新征集风格建议 |
| iPipe trigger / discover / monitor / rerun / release verifier | [24 项专项回归](../../tom-autodev/scripts/tests/test_runtime_profile_guard.py)，含远端 I/O 后和轮询间漂移、对象/字典变化、只读回放 | 当前确认触发已覆盖；作为后续回归 |
| worker / PhaseProtocol / producer 写入 | [execution guards](../../tom-autodev/scripts/tests/test_execution_guards.py)、[profile repin](../../tom-autodev/scripts/tests/test_profile_repin.py) 及两份初审 | workflow 守卫不能替代 profile 证明；列明哪些前驱校验实际支配每个可写入口，哪些直连路径尚无证据 |
| 知识发布与协作 | [KnowledgeSync](../../tom-autodev/scripts/knowledge_sync.py)、[KU](../../tom-autodev/scripts/clients/ku_client.py)、[CollaborationSession](../../tom-autodev/scripts/collaboration.py)；现有 execution guards 覆盖 workflow 漂移 | 长期缓存对象、直接 publication 调用是否重查当前 profile；前置工厂检查能否覆盖后续实际写入；纯回执返回是否真的不写状态 |
| 审批与后台、维护恢复及摘要 | [approval_watch](../../tom-autodev/scripts/approval_watch.py)、[Orchestrator](../../tom-autodev/scripts/orchestrator.py)、[run_summary](../../tom-autodev/scripts/run_summary.py)；现有 workflow 入口矩阵 | 哪些动作可在 profile 不相容时继续产生外部操作或新授权；哪些只是查询、停止、或已经获批的配置修复；逐入口保留证据，不能机械地一律禁止 |

这是一份必要入口清单，不是确认所有入口都存在漏洞。对配置无关的操作，须给出调用链和数据绑定证明；对支持的直连/缓存路径，不能只引用高层 worker 的守卫。真实宿主是否遵守注入约定归入 P，库内可调用入口的约束仍属于 C。

## 明确交付物与结束条件

1. 固定新的完整 commit 和本任务范围，逐入口记录“支持的调用方、读取的配置、上游守卫、新副作用、只读回放条件、现有/新增证据”。无需重审 WF-01…WF-10 其余未变逻辑。
2. 使用真实控制器、临时 Git/SQLite 和模拟边界，覆盖调用前漂移、缓存对象失效、会返回到写操作的远端 I/O 后漂移、批准 repin、未知操作恢复及准确只读回放。每个拒绝场景同时断言远端调用和持久化变化。
3. 若确认遗漏，沿用 A-05 的根因台账，先复现再实施最小修复；正常审批、停止、查询与合法配置修复不得被新守卫卡死。不手改 pin，不弱化测试或标准。
4. 所列入口均有适用证据，确认缺陷已修复，必要回归通过且最终 commit 与证据一致时关闭 `GAP-WF08`。只定向复核这项修复；其他建议进入后续改进清单。

任何新的 P0/P1 必须如实报告。若仍缺必要事实或超过明确的新任务预算，记录具体剩余项并停止；不能靠增加模型数量把不确定性投票成 PASS。
