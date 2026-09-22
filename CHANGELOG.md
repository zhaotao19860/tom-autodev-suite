# Changelog

All notable changes to this suite. Dates are the dates the work landed locally; entries
before 2026-08-27 are reconstructed from design documents and file timestamps, since the
suite had no version control before then.

## 2026-09-22

Round-5 复核后的三项残留修复（复核判定四项 finding 均已闭环，以下处理其残留边界）：

- R5-M2 残留（故障签名路径窄化）：`_normalize_error` 的构建根识别不再只认 `/work|/workspace|/build|/tmp|/var/tmp`，扩展为常见 Linux CI 根（`/home`、`/opt`、`/data`、`/srv`、`/mnt`、`/ssd*`、`/nvme*`、`/jenkins`、`/ci`、`/runner`、`/agent` 等，并支持站点扩展；baidu BGW checkout 位于 `/home` 与 `/ssd*`）。易变构建目录不再局限于根下第一段，任何携带数字/uuid/时间戳的目录段都归一为 `<build-id>`，文件名与非易变源目录保留——同一根因不再因 CI 根或嵌套构建目录不同而跨 run 分裂签名，`api/` 与 `dns/` 等不同测试仍分签。
- R5-H1 残留（在途 run 迁移）：新增 `Orchestrator.recover_legacy_pipeline_plan(run_id)` 及 CLI `recover-legacy-pipeline-plan`，为升级前已进入 IPIPE、payload 无 `pipeline_plan` 的历史 run 从其不可变的、获审的提交与固定 profile 确定性重建并回填冻结计划（与全新 SUBMIT→IPIPE 冻结结果一致）。不提交、不发布、不审批、不调用任何 runtime；在途旧 build 因证据不带计划 binding_hash 会被逐模块重跑，只解 RELEASE 死锁，绝不放行旧 build。幂等（重复调用回放同一结果，不叠加 IPIPE 事件）。IPIPE-entry payload 构建抽为共享 `_ipipe_entry_payload`，保证 SUBMIT→IPIPE 与迁移写出的事件形状一致。
- R5-H2 残留（测试补齐）：新增 DIAGNOSE 阶段错 task_id 的直接回归，断言共享校验在锁定草案前即以 `TASK_ID_MISMATCH`/`content_invalid` 拒绝。
- 文档同步：README 错误码表与升级说明补充 `recover-legacy-pipeline-plan` 的适用场景与语义，`docs/OPERATIONS.md` 恢复命令清单与说明补该命令，`references/failure-signatures.md` 更新已知构建根清单与易变目录段归一规则。
- 验证：控制面 `902` 项通过（本轮新增 `5` 项：CI 根与嵌套构建目录归一 2 项、在途 run 迁移+幂等 1 项、DIAGNOSE task_id 预检 1 项、迁移 CLI 入口 1 项）。未触发真实内网业务构建或发布。

## 2026-09-21

Codex round-5 修复（当前控制策略升级为 `workflow-spec-v2`）：

- R5-H1：提交选择同时匹配当前 Review、Change Set 和 revision set，修复旧提交覆盖新版本以及同 ID 新版本漏提交。全部提交完成后冻结 `pipeline_plan`，调度、构建证据和发布验证共用；同仓多个快照须通过 Git 继承关系证明最终版本，无法证明则 `REVISION_AMBIGUOUS`。默认依赖全仓，显式 `depends_on` 可声明独立模块及传递依赖，产品测试始终纳入。G9 绑定全部必需模块的证据，发布产物保存逐模块平台核验回执；修复、共享测试变化、分支、流水线阶段、环境变化均不能被旧证据替代。相同输入的新回执可复用构建，崩溃后可完成聚合；无计划的历史 run 不允许新增发布成功记录。
- R5-H2：普通和合并草案在 FULFILLED 前复用完整的无审批依赖校验，错误 task、版本、前驱及语义不再锁死 job。历史无效草案只在当前 action 和内容错误均已证明时，通过事务归档旧稿、校验和缓存后 CAS 重开；已发布、合法获审、过期及上下文损坏的稿件保留，修正版使用新审批哈希。
- R5-M1：统一 `execution_guard`，覆盖公开推进、提交、worker、runtime、知识发布、审批后台及 G10 等可写入口，错误在副作用前阻断；只读回执、查询和恢复检查仍可使用。策略版本升为 v2，使旧 v1 run 明确返回 `WORKFLOW_SPEC_DRIFT`，不会沿用旧审批执行新合同。
- R5-M2：故障签名采用 `ipipe-failure:v2:<hash>`，保留长十进制/hex 错误码、case 身份及错误后缀，仅消除明确标记的易变字段；同一失败 stage occurrence 的旧签名以事务一致复用。旧 FailureCase 不自动映射到新签名，提供只读清点脚本与迁移边界说明。
- README、阶段协议和 Producer 合同同步补充冻结计划、依赖声明、无效草案恢复及升级方式；新增真实 SQLite、临时 Git 仓与模拟平台的跨层回归，覆盖修复后旧发布误用、独立模块复用、共享测试、完整回执、版本冲突、断点恢复及入口漂移。
- 验证：控制面 `897` 项通过（本轮新增 `69` 项），tom-review `6` 项通过，tom-diagnose shell 回归与 `git diff --check` 通过。未触发真实内网业务构建或发布。

### 使用文档与 skill 整合发布

- 重写 README：用职责表和流程图解释人工、模型、worker、iPipe 的分工，提供套件安装、一次性项目注册、自然语言入口、CLI 启动/查询和等待原因处理。
- 统一示例从仓库根目录执行；新增 `docs/OPERATIONS.md`，区分 CLI 与 worker 的 `resume/advance`，说明 `submit_draft` 是 Python API、`start` 不启动后台模型，纠正不存在的 `trace` 命令和 `ipipe-rerun --parameter` 用法。
- 首页按当前实现披露修复 hash 合同、尚未接线的单 run 修复预算、跨仓任务边界和发布确认语义；移除过时测试数量与无条件自动恢复承诺。
- 提交包含下述子 skill 补强、内置远程脚本、固定提交范围收集器和可复用评测场景；原外部参考仓库保留，不再作为相关子 skill 的运行时依赖。
- 提交前重新验证：控制面 821 项、Review 收集器 6 项及 Diagnose 隔离回归通过；12 个 skill 校验、20 个文档 CLI 入口与新增本地链接检查通过。原 NPL 规范缺失图片已在索引披露，未修改原文档。

子 skill 后续补强（本轮未改 workflow 核心代码）：

- 统一 producer DraftContent 合同、merged SPEC/DAG 与恢复语义，修复 Review verdict/severity/澄清项语义及 NPL 位宽/overlay 错误硬规则；补 AC 覆盖、C++ 触发/排除与 XFlow 跨层字段合同。
- 将 tom-autodebug 1.5.0 的完整 relay/tmux/进程取证脚本与回归并入 tom-diagnose，保留兼容配置；BGW 远程入口迁移、GitNexus 可选。
- 新增 Review 固定提交范围收集器及隔离 Git 回归，未引入 QA 平台上传链；新增八个可复用模型场景，迁移范围及剩余控制器问题见 `SKILL_CONSOLIDATION.md`。

skill 精简与自包含（减少对外部 skill 的依赖；控制面脚本/契约未变，均为文档/子 skill 内容）：

- 精简 skill 数：把瘦身的 `setup-tom-autodev`（一次性项目注册子 skill）折进 `tom-autodev` 自身——注册能力保留为 `tom-autodev` 的「项目注册（setup）」相位，详情落到 `tom-autodev/references/setup.md`，SKILL.md 增一节短指针，语义不变（`start` 仍要求 `READY` profile；setup 仍是人工监督、不启 run/不生成代码/不触发 iPipe）。删除独立目录 `setup-tom-autodev/`；`scripts/install_links.py` 的自动枚举去掉对该名的显式分支（仅按 `tom-` 前缀）；README 结构/前置/参考文档同步，套件数 13 → 12
- `tom-lang-npl` 自包含化：把 `npl-coder` 的 **NPL 语言**知识并入——`references/npl-docs/` 收 NPL_Specification/Coding_Guidelines/Compilation_Error_Fixup 三件套（字节一致），新建 `npl-idioms.md`（特性门控/强度仲裁/芯片条件编译/flex-editor/总线模型 + Critical Constraints）与 `npl-compile-diagnostics.md`（nlc/xfc 两段式、build.sh 假成功陷阱、6 类 pitfall），`npl-core-rules.md` 增 Hard Language Constraints（无 `return`、`@NPL_PRAGMA` 映射等，已去重），SKILL.md 只加指针；chip PDF / CNA 源 / 运营记忆按边界不并（归 `tom-project-xflow`）
- `tom-review` 自包含化：把 `code-review-qa` 的**语言无关**评审方法论并入（纯 prompt、零运行时依赖）——新建 `review-heuristics.md`（数据流/边界/对抗/契约 + D-01..D-07 语义缺陷，按 Standards/Spec 轴打标）、`security-checklist.md`（OWASP + 业务逻辑 + 置信规则）、`severity-taxonomy.md`（红/黄旗→`severity`+`blocking`）、`false-positive-suppression.md`（sink-first 误报防火墙→`REJECTED_WITH_REASON`/`NEEDS_CLARIFICATION`）、`rule-catalog.md`（G-SECRET/EXCEPT/INPUT/SEC/DB/PERF/LOG/ARCH、BIZ-* 只读检查项）；上传/方舟/Bug-QA/iCafe 拉取/PRD 检索链与全部脚本一律剥离（评审只读、副作用归 orchestrator）
- 相位契约去重：7 个相位 skill 的 `references/phase-contract.md` 收敛为指向父级 `tom-autodev/references/phase-protocol.md` + `phase-artifacts.md` 的指针，仅保留各自独有内容（如 tom-grill 的 `ACCEPTANCE_DELTA_CONFLICT` 与 acceptance-candidates advisory、tom-tasks 的 `business_module`），消除 SKILL.md/契约/父表三处重复维护的漂移面
- 控制面回归 `806` 项仍全绿（本批仅 `install_links.py` 触及脚本，`test_install_links.py` 覆盖通过）

Codex round-3 复核修复（`tom-autodev-suite_review_report_77a933b.md`，分步进行）：

- R-H1：schema 合法但前置绑定错的草案不再锁死 ProducerJob——`worker_driver` 在 `fulfill` 前新增 `_validate_before_fulfill()`，复用协议纯校验 `_predecessor_binding_error`（REVIEW 的 change_set_hash、PLAN/IMPLEMENT revision、DIAGNOSE frozen_revisions、SPEC/TASKS 覆盖）；不符即返回 `retry_allowed` 且不锁 job，修正稿可重投同一 frontier，不再 `PRODUCER_JOB_CONFLICT`。只用绑定校验（不含 envelope/审批校验），未批准的 gated 草案仍能 fulfill 并 park 在其闸门。`_submit_merged` 的 SPEC 半同样前置校验。回归 `807` 项全绿
- R-H2（part 1/2，绑定感知的模块调度）：iPipe 多模块的"已通过"判定由 status/module 粗判改为绑定感知——协议抽出 `_ipipe_passed_modules(events)`（校验 pipeline/release-rule/环境/**revision** 对当前 submission）与公开 `ipipe_outstanding_modules(run_id)`，`_ipipe_outstanding_modules(events, content)` 复用同一核；`worker_driver._next_ipipe_module` 改为委托该协议判定，删除 worker 自己的 status-only `_ipipe_passed_modules`。修复后旧 revision 的成功（被修复改写后）不再被误当完成而跳过该模块（stuck-path A）。回归 `807` 项全绿
- R-H2（part 2/2，崩溃-提交窗口可重放 finalize）：末模块证据已落盘、IPIPE→RELEASE 状态提交前崩溃时，`_next_ipipe_module` 返回 None 但 run 仍在 IPIPE——`_execute_ipipe` 不再 dead-end 于 `IPIPE_NO_OUTSTANDING_MODULE`，而是调 `_finalize_ipipe` 重放最后一份归档 SUCCESS 证据的 ingest（同 module action id 幂等重存，outstanding 空即执行 IPIPE→RELEASE 转移）；无可重放 SUCCESS 证据则回落原停滞报告。回归 `808` 项全绿
- R-M1：merged SPEC+TASKS 在 SPEC 提交后、TASKS job 入队前崩溃的更早窗口现可恢复——`worker_driver._recover_merged_tasks` 从合并 producer job（键于 SPEC action，仍持 `{spec, dag}`）确定性补建 TASKS ProducerJob，`_drive` 的 PRODUCER_WAIT 分支在无 fulfilled TASKS job 时先试恢复，再走 HIGH-001 auto-consume 完成 TASKS，不重唤模型；补该窗口的崩溃注入用例。回归 `809` 项全绿
- R-M6：FailureCase 记账不再是 commit 后 best-effort——`record_failure_case`/`resolve_failure_cases_for_run` 抽出连接内 `_tx` 版，`commit_transition_result` 新增可选 `failure_accounting`，把入库（IPIPE→DIAGNOSE）/解决（RELEASE_SUCCESS）折进与状态提交**同一事务**：无 commit↔记账崩溃窗口、best-effort DB 失败不再丢记，且仅首次 COMMIT 执行（REPLAY 返回已存结果）故计数不翻倍；移除两处 commit 后调用，补 state_store 原子记账 + 重放幂等用例。回归 `811` 项全绿
- R-H3（多模块发布聚合）：`_execute_release` 只 `verify_release` 最后一个 IPIPE 模块会在多必需发布模块场景过早判 RELEASE_SUCCESS——新增 `worker_driver._verify_required_releases`，对 profile 的每个 `required_for_release` 模块（build_id 取各自归档 SUCCESS 证据）逐一 `verify_release`：任一未发布 → park `RELEASE_WAITING`（不强推），必需模块无 SUCCESS build → `RELEASE_EVIDENCE_INCOMPLETE`，全部发布验证通过才落 RELEASE_SUCCESS；无 required 列表（单模块）行为不变。补聚合/等待/缺失/单模块四用例。回归 `815` 项全绿
- R-M5：重复故障路由不再信模型输出的签名——新增 `_authoritative_failure_signature(run_id)` 从归档的运行时 FAILURE 证据取签名，DIAGNOSE 路由的跨 run 逃逸守卫改用该权威签名（仅当无运行时失败证据、如 REVIEW 来源诊断时回落模型签名）；模型即便报不同签名也无法绕过对已知复现根因的升级。补权威覆盖用例（证据 `SIG-real` 已知跨 run，诊断报 `SIG-model-dodge` 仍升级 ARCHITECTURE_REVIEW）。回归 `816` 项全绿
- R-M4：失败签名分离"位置"与"根因"——`_failure_signature` 现在把每个失败 job 的错误消息经 `_normalize_error`（剔除 hex id/hash、路径、数字/行号/时间戳等易变 token）后的指纹纳入签名：同 stage/job 的两个不同错误（不同断言/异常）得到不同签名、不再合并成一个跨 run FailureCase 被误升级；同一根因即便消息只差 build id/行号/时间/路径仍映射同一签名。补"不同错误不合并""同错误跨 build 仍匹配"两用例。回归 `818` 项全绿
- R-M2：workflow_spec 版本漂移守卫——`orchestrator.next()` 先跑 `_workflow_spec_drift`，run 的 pinned `workflow_spec_hash`（G0 冻结）≠ live `canonical_hash()` 时在任何副作用前返回 `WORKFLOW_SPEC_DRIFT`，不再让 run 以 V1 授权静默执行改版后的 V2 策略（scope/gate/transition），须显式迁移或还原 spec；无 pin 的旧 run 不守卫（worker `classify_next` 已把非 ok next() 映射为 BLOCKED 而 park）。补漂移阻断/还原解除用例。回归 `819` 项全绿
- R-M3：replay/跨模型差分按完整 producer identity 匹配——`cross_model_diff` 改为**按 prompt version 分组**、组内比模型（组间不再借用彼此的输出掩盖分歧），整体 DIVERGENT/CONSISTENT/INSUFFICIENT 由各组聚合（例 `[p1/A→X, p1/B→Y, p2/A→Y]` 得整体 DIVERGENT、`groups[p1]=DIVERGENT`、`groups[p2]=INSUFFICIENT_EVIDENCE`，旧的仅按 model 键会误判 CONSISTENT）；`golden_replay` 的 receipt 佐证要求缓存条目 **prompt+model 身份一致**（不再仅凭 output hash 相同），错误 prompt/model 的缓存不能为 receipt 作证。补分组分歧 + 身份佐证两用例。回归 `821` 项全绿
- Codex round-3 复核 9 项 finding（HIGH R-H1/H2/H3、MEDIUM R-M1..M6）全部修复并各带回归用例，控制面回归 `821` 项全绿

Codex round-4 复核修复（`tom-autodev-suite_review_report_ce46b24.md`，多为 round-3 修复的更深边界；分步进行）：

- R4-M2：workflow_spec 漂移守卫下沉为共享纯校验 `workflow_spec.spec_drift(events)`——`phase_protocol._workflow_spec_drift` 在 `_complete` / `_ingest_ipipe_evidence` / `_ingest_release_evidence` 的幂等短路之后、任何副作用之前调用，故直连 `complete-phase`/ingest（绕过 `Orchestrator.next`）也不再能以 V1 授权执行改版后的 V2 策略；只读幂等回放不受影响。补 complete_phase 漂移阻断用例。回归 `822` 项全绿
- R4-H1：merged SPEC+TASKS 在锁定 job 前新增 SPEC↔DAG 一致性预检——`worker_driver._merged_traceability_error` 对手里的 `{spec, dag}` 施加 TASKS 相位同一规则（DAG 的 `acceptance_coverage` 须等于 SPEC 的 `traceability` 验收点集），不符即返回 `TRACEABILITY_MISMATCH`+`retry_allowed`、job 留 PENDING，不再先提交 SPEC 再把 TASKS 卡死成 `PRODUCER_JOB_CONFLICT`。补"错 AC 的 DAG 可重投、一致 bundle 进 G2"用例。回归 `823` 项全绿
- R4-M1：合并恢复只补建"该 SPEC 完成事件产生的" TASKS frontier——`_recover_merged_tasks` 重建前校验当前事件即 SPEC→TASKS 转移（`previous_state==SPEC`、`artifact_id`==该 SPEC 产物、且为 action 的 source event）；故意的 `WORKSPACE→TASKS` 重拆（previous_state==WORKSPACE）不再被旧 DAG 覆盖，转为 park 等新 producer。补"WORKSPACE 重拆不被恢复吞掉"用例，原崩溃恢复用例仍绿。回归 `824` 项全绿
- R4-M4：权威失败签名绑定当前 DIAGNOSE occurrence——`_authoritative_failure_signature` 改读 DIAGNOSE 入口事件：仅 `previous_state==IPIPE` 的诊断有权威运行时签名，取自该事件 `artifact_id` 指向的失败产物；REVIEW 等来源返回 None、回落诊断自带签名。故 IPIPE(S)→修→REVIEW(T)→诊断时按当前 T 路由，不再被历史 S 覆盖。补 IPIPE 来源权威覆盖 + REVIEW 来源不被 S 误升级两用例。回归 `825` 项全绿
- R4-M3：错误归一改为按单元归一易变 token、不再无脑删数字——`_normalize_error` 只把完整 UUID→`<uuid>`、ISO 时间戳→`<ts>`、文件路径(含 `:line`)→`<path>`、长 hex(≥8, build/commit id)→`<hex>`，**保留纯十进制**（401≠503 是根因区分位，blanket 删数字会误合并）；UUID 整体匹配、内部 4 位段不再残留而拆分同一错误。更新 R-M4 跨 build 用例只变 path/hex/uuid/时间，补 401/503 不合并 + 仅 UUID 变化仍同签两用例。回归 `827` 项全绿
- R4-H2（逐模块独立发布）：新增 `IpipeRuntime.verify_release_of_build(build_id)` 按 build **自身绑定**的 revision set 核验，`_verify_required_releases` 改为对每个 required 模块调它——未改模块的已过 build 按它自己构建时的 revision set 发布，不再因兄弟模块改动导致当前整仓集不符而卡在 RELEASE(`REVISION_MISMATCH`)。补 verify_release_of_build 真绑定用例 + 聚合用例改逐 build 核验。回归 `828` 项全绿。（报告在"影响范围"另点出既存的 `_recorded_submissions` 按 change_set_id 取首个可能选到旧 submission——应按当前获审 Change Set 选，属独立子项，待单独处理）

## 2026-09-20

Codex 评审全量修复（分步进行；每步全绿后推进）：

- 上一轮评审补丁：拆超长变更行；`transition_policy.failure_target` 改为委托 `workflow_spec.failure_target`（单一事实源）并移除未用的 `current` 参数；worker best-effort 异常改为记日志；`_gate_settled` 要求具体 input_hash（None 不再匹配任意审批）
- HIGH-002：ProducerJob 草案先校验 schema 再 fulfill——schema 非法草案返回 `DRAFT_SCHEMA_INVALID`（retry_allowed）且不锁定 job，可用修正版重试同一 frontier，不再变成不可恢复的 `PRODUCER_JOB_CONFLICT`；receipt 的 validators_passed 只记真跑过的校验；merged SPEC+TASKS 同样先校验两半再落地
- HIGH-001：审批后 worker 自动消费持久化草案——一次草案 `submit_draft` 记录后，批准其闸门，下一次 `advance()`/`resume()` 直接从持久化草案完成该相位，**不再需要二次 `submit_draft`、不重新唤醒 producer**（提取 `_produce` 共享路径；auto-consume 走同一校验+审批绑定，action_id 键防旧草案越过新审批）
- MEDIUM-002：merged SPEC+TASKS 崩溃可恢复——SPEC 提交后把 DAG 以 checkpoint 落到 TASKS 的 ProducerJob，若在 TASKS 提交前崩溃，`advance()` 从持久化 DAG 恢复 TASKS（复用 HIGH-001），SPEC 不再孤儿、不重跑 producer
- HIGH-003（part 1/2，多模块 iPipe 证据身份）：`ingest_ipipe_evidence` 现在把每模块证据的 artifact action_id 按 module 作用域（`hash(ipipe_action_id, module)`），避免第二个模块的证据撞 `phase_artifacts.action_id` UNIQUE（原 `ARTIFACT_CONFLICT`）；转移仍走 IPIPE source event，同模块重放幂等。
- HIGH-003（part 2/2，worker 逐模块驱动）：`worker_driver._execute_ipipe` 改为一步驱一个 required 模块——`_next_ipipe_module` 从 profile 的 `required_for_release` 集合减去已归档 SUCCESS 证据得到下一个待建模块（`_required_modules` 空则回落 pinned 单模块，保持旧行为），`_ipipe_module_binding` 用该模块自己的 submission `controller_binding`（primary 在 action 上、其余在 IPIPE 载荷 `submissions[]`）derive 触发/证据身份；每模块独立 G7 触发 hash（未结算 park `APPROVAL_REQUIRED`+module）→ monitor → ingest。ingest 返回 `PIPELINE_EVIDENCE_INCOMPLETE` 视为「本模块完成、留在 IPIPE」（`MODULE_INGESTED`），driver 循环续驱下一模块；最后一个模块 ingest 才转移到 RELEASE；任一模块 FAILURE 经 ingest 转 DIAGNOSE。outstanding 每次从归档证据重算，重启从未完成模块继续；required 模块无 submission 则 `IPIPE_MODULE_SUBMISSION_MISSING`，不盲触发。（协议侧多模块聚合 `_ipipe_outstanding_modules`/`_evidence_submission` 已在 part 1 前建好并有测试）
- MEDIUM-003：`golden_replay` 现在强制 receipt.output_hash 必须被某条缓存草案 backing（不仅校验缓存自身哈希），receipt 不能声称一个无缓存草案产生过的输出（receipt 与 cache 的哈希口径本就一致，已验证）；`cross_model_diff` 在已知模型身份 < 2 时返回 `INSUFFICIENT_EVIDENCE`，不再对未知/单模型误报 CONSISTENT
- MEDIUM-001：change_class 与运行策略纳入 G0 绑定并按 run 冻结——`start()` 在 INTAKE 事件里固定 `change_class`、`workflow_spec_hash`(= `canonical_hash()`)、该类的 `workflow_modes` 快照，并以 `intake_hash_version=v2` 把 `change_class`+`workflow_spec_hash` 一并纳入 `g0_input_hash`：只改类别或改类路由/知识 scope（都并入 `canonical_hash`）都会改变 G0 哈希，旧 APPROVE 不可复用；`phase_mode` 运行期改走 `phase_mode_for_run(events, state)`——优先读 run 冻结的 `workflow_modes`，运行中再编辑 `CHANGE_CLASSES` 不会改已启动 run 的闸门/模式路由；model receipt 记录 run 冻结的 spec hash（缺则回落 live）。旧 INTAKE 载荷无 `intake_hash_version` → 仍按 v1 五键哈希，历史 run 不受影响
- MEDIUM-004（part 1/2，稳定签名 + 跨 run 逃逸接线）：`_failure_signature` 改为 occurrence 无关的稳定根因签名——只按 pipeline_id+module+失败 stage/job 的配置身份(stage_conf_id/name)+status 取哈希，剔除 build_id/stage_build_id/job_build_id（无名 job 回落其 build id，故不计入），同根因跨 build/run 现在得到同一签名；DIAGNOSE 完成路由新增确定性跨 run 逃逸——route=REPAIR 时读 `state.failure_case(诊断.failure_signature)`，命中 `repair_policy.known_failure_verdict`（跨 ≥2 run 未解决）则改判 `ARCHITECTURE_REVIEW`(`KNOWN_CROSS_RUN_FAILURE`)，不再盲目再修；IPIPE→DIAGNOSE 真实路径在提交时把该失败签名入 FailureCase 库（不再只有 `orchestrator.route_failure` 才入库）；`orchestrator._record_failure_case` 的静默 `except: pass` 改为记日志。（part 2：修复验证成功后原子标记 resolved + 解决后重置跨 run 计数，下一步）
- MEDIUM-004（part 2/2，修复验证后 resolve + 解决后重置）：run 抵达 `RELEASE_SUCCESS` 时把它命中过的 FailureCase 原子标记 resolved（`state.resolve_failure_cases_for_run`，best-effort、按参与 run 作用域、幂等）；`record_failure_case` 改为「解决后新失败重启跨 run streak」——一个已 resolved 的根因再次首现时 distinct_runs 从 1 重新计（`occurrences` 仍为终身计数），故成功修复后的再现会重新走 REPAIR 而非继续升级 ARCHITECTURE_REVIEW
- MEDIUM-005（多仓 task identity）：`task-dag` 节点新增必填 `business_module`（指名该 task 的目标业务仓）；WORKSPACE 控制器改为按 task 的 `business_module` 选业务仓（多仓精确匹配、单仓无歧义时容忍 advisory module），不再恒取 `business_repos[0]`——第二个业务仓的 task 不会切到第一仓的 worktree，交换 profile 顺序不改变目标；`workspace_binding` 在多仓下强校验 receipt.module == task 声明的 module（不符 `WORKSPACE_TASK_REPO_MISMATCH`），多仓 task 未指名注册 module 则 `WORKSPACE_TASK_REPO_UNRESOLVED` 而非静默默认；新增 `orchestrator.task_business_module` 读取 pinned TASKS 产物。tom-tasks phase-contract 与 phase-protocol 文档同步
- 控制面回归测试 `806` 项全部通过（Codex 评审报告 8 项 finding 全部修复：HIGH-001/002/003、MEDIUM-001..005）

## 2026-09-19

- 瘦身 Stage A（变更类精简）：`standard` 默认合并 SPEC+TASKS——一步产 {spec,dag}、一道 G2、TASKS 免 G3，常规路径少一个模型步与一道闸门
- 新增 `full` 变更类（旧 standard：SPEC/TASKS 分开 + G2+G3），G0 可选，作为复杂/跨仓 DAG 的逃生舱
- 新增 `hotfix` 变更类（express + PLAN 折叠：计划产物仍产、免其 G4；WORKSPACE 绑定 G4 独立强制、不受影响）；`classify_change` 按卡型路由 hotfix/bug/epic → hotfix/express/full，其余 standard
- 瘦身 Stage B（知识/协作写入精简）：新增每相位 KU/iCafe 写入 scope（`workflow_spec.knowledge_scope`）——SPEC/RELEASE(+INTAKE) 落 KU+评论、REVIEW/DIAGNOSE 只落 KU、IPIPE 只发里程碑评论、GRILL/TASKS/PLAN/IMPLEMENT 都不写；`publish_phase(scope=)` 与两处 fail-closed 校验（`_receipt_error` + `artifact_store._validate_final_envelope`）随 scope 条件化；产物始终存 artifact_store（恢复不读 KU）
- README 迁到仓库根并大改（中文详版）：补 `setup-tom-autodev` 注册流程、最新变更类/KU-iCafe scope，及一个从 iCafe 到 RELEASE_SUCCESS 的完整实例（启 worker、切 Comate Agent 填草案、切回、审批、查状态、继续/修改工作流、多工作流并发=单写者租约）
- 控制面回归测试 `774` 项全部通过

## 2026-09-18

- WorkerDriver 自驱 IPIPE 控制器：G7 下 compute-then-approve 推导触发绑定 hash（G7 授权「提交 iCode 并触发 iPipe」，G8 仅失败重跑），触发流水线并 monitor 到终态；成功落 RELEASE、失败转 DIAGNOSE，人工阶段/超时/瞬时故障 park
- WorkerDriver 自驱 RELEASE 控制器：G9 下只读校验平台已发布锁定 build（`verify_release`），未发布 park `RELEASE_WAITING`，成功记发布证据、落 RELEASE_SUCCESS；新增 `release-evidence` schema 与 `ingest_release_evidence`（绑定成功的 IPIPE 前驱）
- Phase 2 持久化与回执：`ModelExecutionReceipt`、每 run 单写者租约（`LockManager.release` + 崩溃 TTL/死 pid 接管）、草案缓存键 `(input_hash, prompt_version, model)`
- Phase 3 语义校验 + 失败复用 + 回放门禁：每 phase 不变量覆盖守卫；跨 run `FailureCase` 库（签名先匹配，跨 ≥2 run 未解决升级架构评审）；`replay_gate` golden replay + 跨模型差分门禁（只读、CI 用）
- 改写 durable-worker 契约（`recovery.py`/SKILL.md/`references/phase-protocol.md`）：WorkerDriver 拥有时序、只在 ApprovalJob/ProducerJob park；有界 Agent 回合仅填 DraftContent
- 计划验收用例：单 standard 需求由 WorkerDriver 全程驱到 RELEASE_SUCCESS（确定性步骤零 Agent 回合、每门一次 APPROVE、事件数远低于历史真实 run 的 102）

## 2026-09-17 — WorkflowSpec 与 durable-worker 安全半 (Phase 1a–1c)

把工作流从“Agent 即执行器”推向“确定性 worker 驱动 + 有界模型 producer”。行为对
standard 类保持不变;新增能力靠全量测试证等价。

- **1a WorkflowSpec 单一事实源**:新增 `workflow_spec.py`,把此前散落 ≥8 处、手工同步
  的工作流语义(状态/转移/G0–G10 闸门/skill/schema/标题/side-effects/证据要求/闸门文案/
  失败路由)集中为一份带版本、可 canonical-hash 的数据。`transition_policy` /
  `phase_protocol` / `evidence_policy` / `approval_summary` 全部改为从它派生;一致性测试
  机械化替代所有“keep in step”注释,消除 437/641 那类漂移。区分 `entry_gate` 与
  `output_gate` 两义(澄清 IMPLEMENT 的 G4/G5)。
- **1b 变更类自适应路由**:iCafe 卡片类型确定性映射出建议类别、由 owner 在 G0 确认/覆盖
  (不新增审批点)。`express` 小改:GRILL 在验收项已存在时自动派生(无模型、免 G1);
  SPEC 与 TASKS 由**一次模型步产 `{spec, dag}`**、经 `submit-draft` 一并落成(SPEC 绑
  G2、TASKS ungated 免 G3),spec 与 task-dag 仍是两份独立产物、下游不变;全部代码/副作用
  闸门(G4/G5/G7/G9)保持。
- **1c WorkerDriver 安全半**:`classify_next`(只读决策)、`execute_auto`(自主完成 auto
  阶段、拒绝非 auto)、`advance`(自动推进确定性阶段 + park + 入队幂等 ProducerJob)、
  `submit-draft`(模型交 DraftContent → 服务端建 envelope → gated 阶段绑定审批后完成,
  无审批则只记草案并返回待批 hash;`express` SPEC 的 `{spec, dag}` 合并在此完成两份产物)。
  确定性步骤零 Agent 唤醒。
- **1c 控制器执行(经确认)**:worker 现在自主执行 WORKSPACE(切 owned worktree、推导 G4
  binding hash、compute-then-approve、批准后 advance WORKSPACE→PLAN)与 SUBMIT(推导评审
  描述符、按其 input_hash 要 G7、批准后提交 iCode)。`advance()` 会自主串联已结算的这两个
  控制器直到下一个模型阶段/未结算闸门/IPIPE。二者均 compute-then-approve、全程受各自闸门
  约束(G4/G7),worker 只在人批准了那笔精确提交后才动作。
- **1c resume(化解 review #3)**:新增 run 级 `resume()`——只看该 run 的 APPROVAL_RESUME
  handoff、校验审批仍有效后完成 handoff 并交给 `advance()`。这修掉了 Stop-hook 全局扫描在
  两个 run 并行时卡死的问题;worker 成为审批结算后的续跑驱动。
- **尚未接线**:IPIPE(触发/监控流水线 + 构造 ipipe-evidence)与 RELEASE 的自主执行——
  这需要新写"从流水线结果构造证据"的逻辑(安全攸关、当前仅测试里手工拼装),宜先设计+评审
  再实现,不在无人值守回合里拼;以及 Stop-hook→worker 的生产接线(把 `resume()` 挂上)。

## 2026-09-17 — 控制面加固基线收绿 (Phase 0)

把 2026-09-07 的加固工作(iCode/iPipe/KU 运行时客户端、提交描述符、iPipe watcher、
审批投递/摘要/watch、阶段文档渲染、profile 重钉、run brief、stage 参数等)收尾到全绿
基线,并修复独立评审发现的两个 CONFIRMED 问题:

- `submit_descriptor` 在查 worktree ownership 前先解析 profile 仓库路径。此前软链根目录
  (macOS `/var`→`/private/var`)会在 `build_and_archive` 已提交评审字节之后仍报
  `WORKTREE_NOT_OWNED`,导致 CLI `submit` 卡死。
- `stage_parameters.redacted()` 改为按 `IREPO-TOKEN:` 头的位置脱敏,不再依赖 UUID 形状
  正则——非 UUID 的 irepo token 不再泄漏进 G8 审批绑定与证据。
- 对齐测试计数,去掉不实的“不再产生 ResourceWarning”表述。

## 2026-09-07

The x86bgw CDN-URL requirement (iCafe `BGW-1956`) was the suite's first end-to-end run. It
finished, but only because an operator worked around the control plane repeatedly, and this
entry is mostly an account of what those workarounds were covering for. The working-tree
state the run actually used is committed first as a single baseline, so everything below
reads as a diff against what ran rather than against the pre-run skeleton. 720 tests pass.

### Added

- The baseline the run used, as one commit: the Infoflow bot gateway and its reply consumer,
  approval delivery/summary/watch, the iPipe watcher, the submit descriptor that makes SUBMIT
  reachable at all, the phase document renderer, profile repinning and the run brief — plus
  the phase-protocol, iCode-runtime and knowledge-sync fixes those exposed.
- Three operator commands for moves the run had to make by hand. `advance` performs a state
  transition, reading `input_hash` and `approval_id` from the approved ledger row for that
  gate — newest wins, so a reissued gate supersedes one that timed out; the run had been
  driving the state machine from `python3 -c` with a pasted hash, mistyped once. The
  evidence list stays the operator's assertion, because those are evidence names rather
  than artifact kinds and deriving them would produce a gate that reads as though it had
  checked the archive. `artifact show` reads an archived artifact by id *through* the hash
  check, where the run had guessed paths under `artifacts/` and `cat`-ed them past it.
  `abandon-intent` releases an external write whose outcome nobody can ever establish, by
  writing an abandonment receipt instead of the `DELETE FROM external_intents` the run ran
  against the live database; the run summary counts abandonments separately so G10 cannot
  read one as a clean external write. It is deliberately not approval-gated — the intents
  that strand a run are often the approval deliveries themselves.
- `SUBMIT -> DIAGNOSE`. The platform's own review (小码哥) and human CR comments both arrive
  after SUBMIT by design, and `IPIPE` was the only exit, so a confirmed defect there left
  the choice between building code somebody had just called wrong and discarding a run
  holding seven approved phases. The edge goes to DIAGNOSE and not IMPLEMENT, so the finding
  is still root-caused before anything changes; the repair returns through PLAN/SPEC as a
  new revision on the same CR.
- `classification` — `CONFIRMED` / `REJECTED_WITH_REASON` / `NEEDS_CLARIFICATION` — required
  on every Review finding, with a `disposition_reason` required for the two non-confirmed
  values, no blocking rejections, and no `ACCEPT` verdict over an unanswered question. The
  skill had demanded this classification since 2026-08-11 while `review.schema.json` had
  nowhere to record it, so the SUBMIT gate could only see `blocking` and "do not implement
  an unverified suggestion" was unenforceable. An unclarified finding now stops the run with
  `REVIEW_NEEDS_CLARIFICATION` rather than going to DIAGNOSE, which root-causes failures and
  would have to invent the verification the reviewer said it could not produce.
- `deviations` required on every change set — required so that an empty list is the assertion
  that the plan was followed, not a field nobody filled in. It rides `candidate_hash` like
  every other field, so a deviation cannot appear after G5 or a Review bound that hash. The
  G5 card now counts the deviations and names their reasons, which is the one thing an
  approver cannot reconstruct from the plan they approved at G4.
- `submit-descriptor.schema.json`, and a `DELIVERY_FAILED` approval state distinct from
  `PENDING`, so a gate whose card reached nobody says so instead of waiting out a deadline.
- `references/failure-taxonomy.md` now separates the three outcomes of an external write:
  nothing was sent, outcome unknown, redrivable. Four of the bugs below were one mistake —
  reading "I do not know what the remote side did" as "a human has to come and rescue this"
  — and `retry_allowed` now states which class a failure is in rather than how the caller
  feels about waiting.

### Fixed

- A pending KU publish intent wedged the run in `RECOVERY_REQUIRED` with the reconciliation
  sitting behind the guard that refused it. Every KU publish step is keyed on the artifact's
  own content hash, so calling `publish_phase` again re-attempts exactly the write that is
  open — unlike `icode.submit` or `ipipe.trigger`, where the only way to learn the outcome is
  to ask. The CDN-URL run hit this nine times, each time when KU's read-back lagged its own
  successful write, and the only way out was calling `publish_phase` by hand. `complete` now
  excludes the operations it re-drives itself; a publish intent naming a *different*
  artifact still refuses absolutely, and so does the post-publish guard.
- Loosening that guard exposed a latent race: the loser of two concurrent `complete` calls
  was refused by `validate_result` before publishing and reported `STALE_ACTION` for an
  action that had in fact completed under the same key. Reconciled at all three points where
  staleness surfaces.
- A dead approval channel aborted the delivery loop, so Infoflow was never attempted and the
  request reached nobody in either place. Every channel is attempted now, and a channel that
  already delivered short-circuits on retry so nothing is double-posted. A defect in this
  process — a missing method, a signature that does not match — withdraws its own claim and
  returns a retryable `APPROVAL_DELIVERY_FAILED`, while a transport error still leaves the
  claim open to be reconciled; the failure is recorded on the ledger row, so an unanswerable
  gate drops out of the reply watcher and can be reissued.
- Artifacts archived through the plain `ArtifactStore.put` were hashed, indexed and read back
  as evidence with nobody having looked at their shape — including the submit descriptor
  iCode is asked to take and the run summary G10 reasons from. A descriptor missing
  `revision_set` was found by iCode rejecting the submission, one approved G7 gate and one
  push attempt later. Rejection now happens before any write, so nothing can cite an
  artifact that was refused: no file, no index row, no id.
- `run-summary.schema.json` and `optimization-proposal.schema.json` described documents that
  have never existed — a required `result` and `phase_timings`, a flat `target_files` of
  relative paths. Nothing loaded them, so the drift cost nothing and their fixtures tested
  the fiction. Both are rewritten from the builders in `scripts/run_summary.py` and both are
  now loaded. `_validate_optimization_targets` went with them; as written it would have
  called every genuine candidate forbidden.
- `with sqlite3.connect(...) as connection` ends the transaction and leaves the handle open
  until the collector gets to it, and each store opens one per method, so a run leaked a
  descriptor per call and the suite printed a wall of `ResourceWarning`s that hid real
  output. Fixed at each class's single `_connect` factory, which leaves all ~40 call sites
  unchanged and subsumes the two that had grown a hand-rolled `try/finally` around this same
  bug.
- The submission frontier joined on "some change set of this task was submitted", so a
  repaired task looked settled by its old receipt and the run could reach IPIPE on a
  sibling's submission with the repair still in the worktree. It now joins on the task's
  current change set, and `artifacts_for_run` breaks a `created_at` tie on `rowid` rather
  than a content hash, so "the last descriptor" means the one last written.

## 2026-08-27

### Changed

- Collected the 13 previously sibling skill directories under this single
  `tom-autodev-suite/` container. Host skill discovery is flat, so the three host
  directories (`~/.comate/skills`, `~/.codex/skills`, `~/.claude/skills`) keep flat
  symlinks by the same names, now pointing one level deeper. 38 links repointed and
  verified; `install_links.py --dry-run` reports 38 `UNCHANGED`.
- Updated the three hardcoded paths that referenced the old flat location:
  `tom-autodev/tom-autodev-ku.md`, `tom-autodev/tom-autodev-share.md`, and the embedded
  markdown inside `tom-autodev/ku-how-to-use-operation.json`.

Project profiles were untouched: `language_skill` and `project_skill` point at the host
symlink paths, which did not change. `validate_profile` on the bgw profile still returns
`READY`.

### Fixed

- Four test modules failed at import with `ModuleNotFoundError: No module named 'scripts'`
  because they imported sibling test modules as `scripts.tests.X` while the suite has no
  `scripts` package and each module already puts `scripts/` on `sys.path`. `unittest`
  recorded the import errors and skipped the modules, so 96 tests had never run. Switched
  the five imports to the plain sibling form already used by `test_fake_e2e.py`. Collected
  test count went from 341 to 437; all newly enabled tests pass.
  Affected: `test_knowledge_sync.py`, `test_phase_protocol.py`,
  `test_phase_protocol_repair.py`, `test_task7_controller_boundary.py`.
- `test_task5_safety.test_real_gateway_client_orchestrator_chain_accepts_pending_and_nested_reply_once`
  pinned `now` to an absolute `2026-08-11` and set the approval deadline 24 hours later.
  The injected gateway clock honored that, but `ApprovalLedger.receive` judges deadlines
  against the real clock, so once the wall clock passed 2026-08-12 the approval resolved to
  `TIMEOUT` instead of `APPROVE`. Made the test's `now` relative to the current time. The
  production timeout behavior is correct and was not changed.

### Added

- `README.md` and this changelog.

## 2026-08-13

### Changed

- Control-plane implementation reached its current shape: write-once artifact store with a
  SQLite phase index, KU knowledge sync with receipt verification, G0-G10 approval ledger
  with dual-channel first-valid-response semantics, worktree ownership reconciliation, and
  iCode/iPipe runtimes pinned to a validated project profile.

## 2026-08-11

### Changed

- Phase skills (`tom-grill`, `tom-spec`, `tom-tasks`, `tom-plan`, `tom-implement`,
  `tom-review`, `tom-diagnose`) aligned on the shared `ArtifactEnvelope` contract: every
  phase consumes and emits a schema-validated, content-hashed envelope, persists changed
  documents to KU, and links the artifact from an iCafe comment.

## 2026-08-10

### Added

- Executable control-plane design and implementation plan
  (`tom-autodev/docs/superpowers/`): explicit state machine, evidence gate before every
  side effect, failure taxonomy with bounded repair rounds, and post-run G10 optimization
  restricted to approved suite roots.

## 2026-07-15

### Added

- Initial suite: `tom-autodev` controller, `setup-tom-autodev` registration flow, the
  language skills (`tom-lang-c-cpp`, `tom-lang-npl`) and the project skills
  (`tom-project-bgw`, `tom-project-xflow`).
