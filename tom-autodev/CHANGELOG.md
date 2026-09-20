# 发布记录

本 skill 属于 tom-autodev 套件，整套共 13 个，必须一起安装才能完成端到端流程。套件构成与各自职责见 SKILL.md 的依赖说明。

## 2026-09-19

- 瘦身 Stage A（变更类精简）：`standard` 默认合并 SPEC+TASKS——一步产 {spec,dag}、一道 G2、TASKS 免 G3，常规路径少一个模型步与一道闸门
- 新增 `full` 变更类（旧 standard：SPEC/TASKS 分开 + G2+G3），G0 可选，作为复杂/跨仓 DAG 的逃生舱
- 新增 `hotfix` 变更类（express + PLAN 折叠：计划产物仍产、免其 G4；WORKSPACE 绑定 G4 独立强制、不受影响）；`classify_change` 按卡型路由 hotfix/bug/epic → hotfix/express/full，其余 standard
- 瘦身 Stage B（知识/协作写入精简）：新增每相位 KU/iCafe 写入 scope（`workflow_spec.knowledge_scope`）——SPEC/RELEASE(+INTAKE) 落 KU+评论、REVIEW/DIAGNOSE 只落 KU、IPIPE 只发里程碑评论、GRILL/TASKS/PLAN/IMPLEMENT 都不写；`publish_phase(scope=)` 与两处 fail-closed 校验（`_receipt_error` + `artifact_store._validate_final_envelope`）随 scope 条件化；产物始终存 artifact_store（恢复不读 KU）。iCafe 不再每相位刷屏、KU 只沉淀有复用价值文档
- README 大改（中文详版）：补 `setup-tom-autodev` 注册流程、最新变更类/KU-iCafe scope，及一个从 iCafe 到 RELEASE_SUCCESS 的完整实例（启 worker、切 Comate Agent 填草案、切回、审批、查状态、继续/修改工作流、多工作流并发=单写者租约）
- 控制面回归测试 `774` 项全部通过

## 2026-09-18

- WorkerDriver 现在自驱 IPIPE 控制器：在 G7 下 compute-then-approve 推导触发绑定 hash（G7 授权「提交 iCode 并触发 iPipe」，G8 仅用于失败重跑），触发流水线并 monitor 到终态；成功落 RELEASE、失败转 DIAGNOSE，人工阶段/超时/瞬时故障则 park 交人处理
- WorkerDriver 现在自驱 RELEASE 控制器：在 G9 下只读校验平台已发布锁定的 build（`verify_release`），未发布则 park `RELEASE_WAITING`，成功则记录发布证据并落终态 RELEASE_SUCCESS
- 新增 `release-evidence` schema 与 `ingest_release_evidence` 摄入：发布证据绑定到成功的 IPIPE 前驱（同 pipeline/module/revisions/release rule/environment/build），确保只能对流水线证明过的 build 发布
- `execute_controller` / `advance` 在注入 iPipe API transport 时同时驱动 IPIPE 与 RELEASE，打通 INTAKE→…→RELEASE_SUCCESS 的 worker 驱动闭环
- 新增计划验收用例：单个 standard 需求由 WorkerDriver 全程驱动到 RELEASE_SUCCESS，断言 ProducerJob 次数=模型 phase 数(6)、四个副作用控制器(workspace/submit/ipipe/release)全在 worker 循环内自动执行(确定性步骤零 Agent 回合)、每道闸门一次人工 APPROVE、事件数远低于此前唯一真实 run 的 102
- 改写 durable-worker 契约(recovery.py 的 `next_step`、SKILL.md、references/phase-protocol.md)：WorkerDriver 拥有时序并执行全部确定性步骤与控制器副作用，只在 ApprovalJob/ProducerJob 处 park；有界 Agent 回合仅填充 ProducerJob 的 DraftContent，不再"由 current Agent 执行整条序列"
- Phase 2 持久化与回执：每次 ProducerJob 填充记 `ModelExecutionReceipt`（input/output hash、prompt/spec 版本、validators）；WorkerDriver 可选每 run 单写者租约（`LockManager.release` + 崩溃持有者 TTL+死 pid 被接管）；草案缓存键 `(input_hash, prompt_version, model)` 供再驱动复用
- Phase 3 语义校验 + 失败复用 + 回放门禁：每 phase 不变量覆盖守卫（语义/结构-only 两集合须划分全部命名 schema）；跨 run `FailureCase` 库（按签名累积、`route_failure` 记录），`repair_policy` 签名先匹配——跨 ≥2 run 未解决的失败升级 ARCHITECTURE_REVIEW 而非再修；`replay_gate` golden replay（草案确定性/完整性）+ 跨模型差分门禁（同输入不同模型输出分歧则 DIVERGENT，只读、CI 用非运行时拦截）
- 控制面回归测试 `773` 项全部通过

## 2026-09-15

- IPIPE 失败或人工阶段先读取项目 Skill 的拓扑、日志和参数映射，再由控制面解析产物 URL、脱敏 token 并执行 `ipipe-rerun`
- BGW `P0新case回归` 参数 `get_bgw_test_case` / `get_bgwagent` 按模块别名解析，不再用 basename `x86bgw` 撞业务仓

## 2026-09-08

- 修复 iPipe 多模块证据聚合、build/stage ownership 持久化、旧 revision 误用和 stage endpoint fallback 问题；补充 module/build/stage 证据引用及按 module 配置 release rule
- 修复 watcher、审批提醒、超时重发、确认回执和 IDE handoff 通知的并发重复发送；未知外部结果统一进入查询态
- 修复状态迁移与幂等写入的原子性，增加旧版 Review/Change Set artifact 兼容校验
- 增加 `ipipe-rerun` CLI，修正 Comate Stop hook 文档，并让 Infoflow 协作群在 bot agent 缺失时快速失败
- 控制面回归测试 `641` 项全部通过

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28434`
- 发布范围由空间可见调整为广场可见
