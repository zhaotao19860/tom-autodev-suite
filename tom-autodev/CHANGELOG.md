# 发布记录

tom-autodev（控制面）的变更摘要，按日期倒序。完整说明见套件根目录 [CHANGELOG.md](../CHANGELOG.md) 的同日条目。

## 2026-10-28

- MVP 评审修复第 1 轮：iCode 新 CR 缺席跨 NEW/MERGED/ABANDONED 判定；trigger 确认只认晚于 intent 的 build；多仓聚合 PARTIAL/冲突分类修正；`intent_state` 增加 REFUSED；scope 台账策略对齐与顺序无关；stage 配置快照纳入默认值、上游参数、job 组顺序与命令 hash；watcher 指纹覆盖 schemas。详见根 CHANGELOG 同日条目。

## 2026-10-25

- B-23：同 stage/模块之后出现的 5xx 或缺状态码下载结果不再覆盖先前已证实的 403/404；只有成功下载才解除环境拦截（`environment_evidence.proven_dependency_failure`，测试 `test_b23_an_inconclusive_later_attempt_does_not_clear_a_proven_404`）。
- B-21：依赖失败证据按 stage 绑定、逐次追加（`ipipe.dependency-failure:{run}:{build}:{stage_build_id}:{module}:{attempt}`，含
  `stage_conf_id`、声明参数、消费该模块的参数名），只有被诊断失败的 stage 正是记录中的 stage、其声明消费该模块、且最新一次尝试全部
  403/404 才权威；5xx/缺状态码只记 `advisory`；`ipipe-rebuild` 探测不再写记录；`--print-parameters`/`--print-input-hash` 只读不写。
- B-22：8 个结构化环境字段（`errorType`/`agentStatus` 等）文档改为候选映射，未经真实 iPipe 返回验证，当前实际不生效；代码映射保留。
- 剩余风险（待负责人决定，未接受）见根 CHANGELOG 同日条目。
- 评审第二轮：权威环境分类只认强证据（结构化平台字段、控制面证实的依赖获取失败、`ENV_*`/控制面答复），关键词命中降为参考类
  `ENVIRONMENT_SUSPECTED`；权威环境类下 `REPAIR`/`SPEC`/`GRILL` 一律 `ENVIRONMENT_REPAIR_FORBIDDEN`。另修 A-11（IREPO-TOKEN 空白/等号）、
  A-12（新草稿先脱敏）、A-13(b)（被其他回执认领的同 id stage 需更晚 startTime）。
- 补完：依赖失败证据只有 `ipipe.dependency-failure` 一个来源（`ipipe-rebuild --request-g8` 探测 403/404 也写入它，G8 材料不再当证据）；
  产物下载须每个精确候选都有 403/404/5xx 状态码才记录；脱敏后与已记录草稿相同的重提按已记录内容消费；`ipipe_watch` 修复方向补充
  `ENVIRONMENT_SUSPECTED`/`ENV_*`。
- 评审跟进：`ipipe-rebuild` 调用点本地错误以 `local_error_proven` 关闭 trigger intent，无法记录时返回
  `REBUILD_LOCAL_ERROR_UNRECORDED`（不再静默悬挂）；`job_override_decision` 统一 G8 材料准备与执行时的 job 级参数判断，
  继承参数在材料阶段即 `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`；`select_product` 对精确 revision 候选逐个校验下载，全部失败才
  汇总状态码返回。
- 评审缺陷修复（根 CHANGELOG「两份独立评审确认缺陷修复」）：
  - `append_existing_cr` 提交在 push 后可确认并写回执（A-03）。
  - CODE_ONLY 修复回 IMPLEMENT 时 pin 已接受 Plan、baseline 取 Plan revision（B-01）。
  - 被取代/被拒 build 不再作为模块、发布、RELEASE 前驱或 G9 证据，其 stage 不能重跑；IPIPE 态重建需失败证据或产物失效证明（B-05）。
  - rerun"已启动"需要晚于 intent 的 startTime，后继 stage 不可被多次认领（A-01）；同 stage 并发 G8 最多一次 POST，CLI 持 run 级租约（A-05）。
  - `--local-error-proven` 需要 `before_dispatch: true`（A-02）；`TRIGGER_MODE_MISMATCH` 的新 build 标记为 `ipipe.build-rejected`（A-04）。
  - 持久化前统一脱敏凭据，值级检查拒绝未脱敏凭据（A-06）。
  - REPAIR_LIMIT 按 run 累计，ARCHITECTURE_REVIEW 只重置升级/无进展计数（B-02）。
  - `adopt-code-only-repair` 补齐预算继承、最新诊断/已接受 Plan、幂等/冲突、task 绑定回执与受控迁移（B-03）。
  - 环境类失败以运行时分类为准禁止 REPAIR，`PIPELINE_TRANSIENT` 不可修复（B-04）。
  - 文档对齐（B-06/B-07/B-08）。workflow spec hash 未变。
- 显式命令消费审批后完成对应 `APPROVAL_RESUME` handoff，消除 `MULTIPLE_RESUME_HANDOFFS`。
- 新增 `ipipe-rebuild`：编译产物失效后对同一 CR 版本受控重新构建（新增 `DIAGNOSE -> IPIPE` 边，曾改变 workflow hash）。
- 补记此前已落地、未入账的变化（落地日期未记录）：CODE_ONLY 修复直接回 IMPLEMENT、ARCHITECTURE_REVIEW 预算边界（已于 10-25 修正语义）、环境失败禁止
  REPAIR 守卫、`adopt-code-only-repair`、`references/changelog-policy.md`、`MULTIPLE_COMMITS_ABOVE_BASELINE`、
  `_architecture_reentry_task`。

## 2026-10-23

- 未变更模块发布产物必须与冻结 revision 一致（`PRODUCT_REVISION_MISMATCH`）；本地编程错误放弃的 rerun 可在同一审批下重发一次。

## 2026-10-22

- iPipe 重跑：本 stage 自有参数走 `manual_execute_stage`，本地错误不再留下"结果未知"的 intent。

## 2026-10-14

- iPipe 重跑未知结果保留脱敏证据，reconcile 区分"未启动但未证明被拒"；同一阶段可在新 G8 下再次重跑。

## 2026-10-12

- 人工 iPipe 阶段自动发现参数并准备 G8。

更早的条目见根目录 [CHANGELOG.md](../CHANGELOG.md)。
