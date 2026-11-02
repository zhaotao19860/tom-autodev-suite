# 套件评审记录 R11B

**结论先行：本片 FAIL，C 退出状态为 BLOCKED。确认 8 项缺陷：7 项 P1、1 项 P2，均未修复。** 未修改仓库，未调用真实平台，未执行业务项目编译或测试。

## 基线与范围

- **标准**：TAD-REVIEW-EXIT / 1.0；已读取 AGENTS.md，标准 SHA-256 匹配：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- **仓库/分支**：tom-autodev-suite / mvp；base_commit：全量；target_commit：`9b8daff8a7693a14a3c150625ed67a688ff2a381`。HEAD 匹配，评审前后工作区无改动。
- **目标/范围**：C 全量评审第 1 轮、分片 B；限用户指定的 11 个文件及必要调用方，检查外部写入、审批绑定、发布证据、restart-from、ENV_WAIT。
- **环境**：macOS 26.6.2 arm64、Python 3.14.4、SQLite 3.53.0；文件系统只读，包括 `/tmp`。
- **规则身份**：
  - workflow：`workflow-spec-v2`，canonical hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`。
  - `tom-autodev/SKILL.md` SHA-256：`d212a9d7d8e1ecbec2354497dae83b48dfd552ea2a40e2f2a5a767766f26da99`。
  - 16 个 schema 的路径排序合并指纹：`c746d7f5bde93c2a68072854a86fd979d9b6d41f18b0612cee2917dcfa41c5d4`。
  - profile 使用目标提交中的 `test_ipipe_runtime.PROFILE`、`test_orchestrator.PROFILE`；无生产 profile、无模型 producer/prompt 执行。
- **评审者/日期**：Devin，具体模型版本未知；2026-11-02。
- **负责人/预算**：发起人 tom；本片初审 1 次，修复复核 0 次；默认最多两轮修复→复核。
- **继承证据**：不继承历史 PASS；整合报告、评测记录、CHANGELOG 仅作线索，以下缺陷均核对当前代码并交叉复现。

以下代码路径均相对 **`tom-autodev/scripts/`**；WF 合同指评审标准 §2，MVP 合同指 `docs/MVP_CONVERGENCE_PLAN.md`。

## 验收矩阵

| WF / 场景 | 实际证据与结果 | 状态 |
|---|---|---|
| WF-02：G7/G8/G9 精确授权 | 核对 run、gate、hash、有效决定守卫；追加 CR 身份漂移后仍写入，见 R11B-002 | **失败** |
| WF-04/05：未知结果、幂等与恢复 | 内存探针复现超时后重发、错误 rebuild 归因、restart 漏查及链式重放失败 | **失败** |
| WF-03/06：冻结分支与构建身份 | 平台明确返回其他分支，仍保存为 main 并监控成功，见 R11B-004 | **失败** |
| WF-07/10：发布唯一身份、证据归属 | 多 release 被取最新；外模块 build、外 stage detail 均生成成功 proof | **失败** |
| WF-08：漂移保护 | 静态核对本片入口守卫；完整漂移回归及全部可写调用链未运行 | **未完整验证** |
| WF-04：ENV_WAIT | `test_env_wait` 8 项，替换内存夹具：复查节流、顺延、新证据解除、到期 STOP、STOP 补偿、权限/未知状态不等待均通过 | **通过所列场景** |
| WF-09：restart 预算继承 | 纯内存 A→B→C：A 用 2 轮、B 再用 1 轮，C 继承 3；环境诊断不计入 | **通过该场景** |
| 正向回归子集 | 原样：`test_product_policy` 24 项、iPipe HTTP 3 项、证据映射/签名 9 项；内存夹具：intent 12、submission_set 14、iPipe runtime 71、ENV_WAIT 8、compile 恢复 1，均通过 | **通过子集，非完整 C** |

复现均通过不落盘的 `python3 -c`、`:memory:` SQLite 和 fake 平台执行；保留生产业务方法，替换必要的文件系统/Git/平台事实。以下输出证明控制面缺陷，不是实际平台提交或发布记录。

## Findings

| ID / 严重级别 | 违反合同与文件:行 | 失败场景：预期 → 实际及影响 | 当前证据与反证核对 | 建议修法 / 状态 |
|---|---|---|---|---|
| **R11B-001 / P1** | **WF-04/05；MVP §3.1**。`clients/icode_runtime.py:424–431`；`state_store.py:1911–1914`。必要调用方：`cli_transport.py:66–69` | `push_cr` 已发出，等待应答超时。应保留 UNKNOWN、只读核对；真实 `ProcessTransport` 却将超时转换为退出码 124，runtime 将其写成终态 `SUBMIT_REJECTED`，下一次调用立即开新 intent 再推送，盲目重复副作用。 | 使用真实 `ProcessTransport` 的超时转换，mock `subprocess.TimeoutExpired`，平台列表暂未出现 CR：两次 submit 得到 **`pushes=2`、状态均 REFUSED、`pending=0`**。已有 timeout 测试直接抛异常，未经过生产 transport 的 124 返回路径；absence grace/fence 也不保护该拒绝回执路径。 | 区分已证明未发送的本地拒绝与发送后未知；124、信号退出、应答丢失进入 UNKNOWN。只有核对确认未发生后才允许重发。**确认，未修复。** |
| **R11B-002 / P1** | **WF-02/03/08：批准仅限准确动作及目标身份**。`clients/icode_runtime.py:881–886`，写入点 `:405–414` | G7 批准“向 main 的 CR #41 追加 patchset”，平台随后返回 #41 已在 release 分支。应拒绝目标漂移；代码仅检查 CR 号和 Change-Id，就返回 `CR_APPEND_ALLOWED`，继而执行 `push_cr --branch main`，可新建另一条 CR，超出追加 #41 的授权。 | 完整 submit fake 复现：原 #41 属于 release，实际 **push 1 次并创建 main 的 #99**；事后才返回 `SUBMIT_CONFIRMATION_REQUIRED`。descriptor/G7 哈希检查通过；`_remote_drift` 只核对 Git 分支祖先关系，不核对 CR 分支；事后 UNKNOWN 不能撤销已发生的越界写入。 | append 放行前核对目标 CR 的 branch、module、owner、卡片及 Change-Id；明确冲突即阻断，不能仅凭 CR 号放行。**确认，未修复。** |
| **R11B-003 / P1** | **WF-04/10；MVP §3.1：rebuild 与 trigger 相同的创建时间归因**。`clients/ipipe_runtime.py:1941–1943`、`:3649–3666` | rebuild POST 超时，结果 UNKNOWN。之后才可见候选 build 101：①没有可用时间；②创建早于 intent 一小时、启动晚于 intent。两者均不能证明属于本次写入，应保持 UNKNOWN；实际均确认 101 并写成功回执。 | 两种探针均为 **`reconcile_ok=true, adopted_build=101, receipt_saved=true`**，POST 各仅一次。同对象经 `_created_after` 均为 false。原 build 排除、数字 ID 和完整版本匹配均通过；根因是只排除“证明启动过早”，把“未证明过早”当成“证明属于本次”。 | 对已 dispatch 的 rebuild 使用正向创建时间归因；优先使用平台创建/trigger 时间，缺有效时间保持 UNKNOWN。保留明确的写入前 adoption 路径。**确认，未修复。** |
| **R11B-004 / P1** | **WF-03/06/10：构建必须匹配冻结分支**。`clients/ipipe_runtime.py:2876–2895`；身份保存 `:2632–2643` | 冻结目标为 main；平台 build 的 `branch` 和 `trigger.branch` 均为 `dev-with-skips`，但 module、pipeline、完整 revisions、params 匹配。应拒绝错分支；实际 discover 成功，保存的 binding 被填成 main，monitor 返回 SUCCESS。其他分支的配置/跳过规则结果可冒充目标分支验证。 | 实测：**`platform_branch=dev-with-skips`、`persisted_binding_branch=main`、`build_matches=true`、`monitor_status=SUCCESS`**。未依赖缺 revision/params 的宽松行为。上游只检查 profile 与请求自洽；下游检查的是从请求保存的 branch，而非平台实际 branch。 | 在 discover、确认和 monitor 接纳 build 时核对实际分支；平台显式冲突必须拒绝，不能用 context 回填为“已验证身份”。**确认，未修复。** |
| **R11B-005 / P1** | **WF-07/10；MVP §3.1：“同 revision 多个 release → AMBIGUOUS”**。`clients/ipipe_runtime.py:2344–2348`、`:2359–2389` | 同 module、revision、branch 的两个 BranchPipeline build 分别有 release-A、release-B。应报告发布身份歧义；实际先选最新 build，只检查 release-B，随后保存成功 proof。 | 真实 `verify_planned_release` 内存复现：**`release_id=release-B, saved_proof=true`**，仅查询最新 build 的 release stage。已有 `len(releases)>1` 守卫只统计**单个已选 build 内**的 release，其他 build 在进入守卫前已丢弃；下游只核对已保存 proof。 | 汇总匹配候选 build 的 release 身份，多身份返回 AMBIGUOUS；或采用明确、持久化且受审批绑定的唯一 branch-build 身份，不能默认“最新即本次”。**确认，未修复。** |
| **R11B-006 / P1** | **WF-07/10：发布证据必须属于正确模块及对象**。`clients/ipipe_runtime.py:2333–2348`、`:2374–2388` | 两种输入：①列表项 revision/branch 相符，但明确属于外 module/pipeline；②build 正确，但 stage detail 明确属于外 stage/build/pipeline，release job 又属于外 module。应拒绝身份冲突；实际均把 `foreign-release` 当成目标模块成功发布。 | 两种 `verify_planned_release` 探针均输出 **`ok=true, proof_module=baidu/team/app, proof_saved=true`**。真实 API client 只解包，不补归属校验；ChangePipeline 的 `_matches_build` 不覆盖此分支。`pipeline_plan.release_binding_error` 比对的是 runtime 用 target 生成的 proof，无法识别被覆盖的源响应冲突。 | 逐层核对 BranchPipeline build、stage detail、release job 的归属；显式冲突立即拒绝，缺关键身份时权威回查补齐，确认后才能保存 proof。**确认，未修复。** |
| **R11B-007 / P1** | **WF-04/05；MVP §3.4：每个远程对象均须处置，在途 build 须结束/取消确认**。`restart.py:112–122`、`:242–245` | iCode 已确认提交并自动启动 ChangePipeline，或合入后启动 BranchPipeline；worker 尚未 discover/bind，所以没有 `ipipe.build-binding`。应发现运行中的 build 并阻断；实际仅枚举本地 binding，得到空列表，直接重开。 | 真实 plan/execute 探针：CR 为 MERGED、`accept_merged=True`，模拟平台存在 RUNNING build；结果 **`RESTART_READY → RESTARTED`、`builds=[]`、状态查询次数 0**。自动构建是现有支持路径（`worker_driver.py:1143–1147`）；租约和 intent/event CAS 只能约束本地写，不能发现平台自动对象。 | 根据确认提交的 module/revision/CR 发现关联 Change/BranchPipeline，与本地 binding 取并集；发现不完整或状态未知即阻断。`--accept-merged` 不能替代 build 处置证明。**确认，未修复。** |
| **R11B-008 / P2** | **WF-04/05；MVP §3.4：重复重开必须返回已有 target**。`state_store.py:515–517`；消费者 `restart.py:218–221、307–313` | 已有 A→B→C，B 同时有入边和出边。`lineage(B)` 用 OR 查询后直接取首行；SQLite 改选表扫描时返回 A→B。应重放既有 C；实际 `plan(B)` 错报 READY，execute 返回 `ok=false`，不再返回 C。 | SQLite 3.53.0 内存实测：ANALYZE 前为多索引 OR，重放正常；后为 **`SCAN run_lineage`、`lineage(B).source=A`、execute(B)失败**。唯一约束仍防止重复建 run，预算也未刷新。已验证只读按 `source_run_id=B` 精确查询仍可找回 C，故影响有限、有绕行。未声称生产库已触发。 | 为出边/入边提供明确查询；restart 只查 source 出边，不能依赖 OR 首行顺序。绕行是精确查询并使用既有 C。**确认，未修复；风险接受待负责人决定。** |

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/处置 | 负责人及接受决定 | 到期/重开条件 |
|---|---|---|---|---|
| R11B-001～007 | 未解决的核心写入/证据 P1 | 修复并定向复核；不能以风险接受替代 P1 修复 | tom；待处理 | C 退出前 |
| R11B-008 | 链式 restart 重放局部缺陷 | 精确查询既有 target 后使用；修复或明确延期 | tom；尚无接受记录 | C 退出前决定 |
| B-L01 | `/tmp` 不可写，未原样运行完整 iCode、restart、pipeline_plan、rebuild 落盘集成及并发矩阵 | 内存子集不替代文件锁、跨进程和落盘恢复验证；另有 2 个 API shape 用例因 tempfile 创建失败未完成 | tom；待补证据 | 完整 C 汇总前 |
| B-L02 | A/C 片、模型资格及真实平台不在本片结论内 | 不推导其通过，不将 fake 结果冒充平台证据 | 总评审负责人 | 合并报告及相应资格验证 |

## 结论

- **C：BLOCKED，仅就 B 片而言。** 7 项确认 P1 未解决，另有 P2 待处置及上述覆盖缺口。
- **M：未验证。**
- **P：未验证。**
- 本轮是全量评审的分片初审，不是局部修复复核，也不代表 A/C 片结论。
- 停止本轮扫描；下一步仅处理确认缺陷、对应回归和影响面复核，不追加风格或额外功能验收目标。

**本片结论：FAIL。**