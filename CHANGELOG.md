# Changelog

All notable changes to this suite. Dates are the dates the work landed locally; entries
before 2026-08-27 are reconstructed from design documents and file timestamps, since the
suite had no version control before then.

## 2026-10-31 · 监控：他人重新执行的 stage 不得顶替本 run 经 G8 启动的执行

- `IpipeRuntime._prefer_owned_stage_runs`：stage 列表只给出每个 stage 的最新一次执行。若本 run 已在该 build 上经 G8 启动某 stage，监控改按该次执行的 stage build id 读回状态与 job，另一次执行（`foreign_execution`）不计入证据。
- 起因：BGW-2019 S5 目标 run b9a99c4c，G8 重跑 p0级自动化回归测试（stage 651470513，回归仍在运行）4 分钟后，同一 stage 又被执行一次（651471226，全部 job 按表达式跳过、1 秒结束）；列表只剩后者，build 报 SKIPPED，控制面据此按通过入账并进入 RELEASE。该 run 的 IPIPE 证据因此不可信，需待 651470513 跑完后重新核对。

## 2026-10-31 · S5 RESTART 真实验证

- BGW-2019 run 3dfe403a → b9a99c4c：已合入 CR 的阻断、`--accept-merged` 处置、原子提交与回放都按合同 3.4 生效；目标 run 重走 GRILL/SPEC/TASKS（全部经预授权门）并提交新 CR。`docs/MVP_CONVERGENCE_PLAN.md` 第 8 节同步。
- 同一 run 的 P0新case回归再次因配置写死的 qa 产物 404 失败，失败签名与 run dec4229f 相同，经 G8 用本 run 产物重跑后按环境诊断回到 IPIPE。

## 2026-10-31 · S4 真实验证：提交结果未知时 continue 自动收敛

- `worker_driver._drive`：SUBMIT 阶段存在未回执的 `icode.submit`（推送后、回执前进程中断）时，`continue` 先回放该任务已批准的提交（`_submit` 只读回查，已确认的仓不重推），而不是一直停在 `RECOVERY_REQUIRED`。此前只能手工执行 `orchestrator.py submit`。
- BGW-2019 第三轮（run 3dfe403a）受控制造：qa CR 123064384 已确认后，x86bgw 的 `push_cr` 成功执行但回执前抛错。观测到聚合状态 PENDING（x86bgw UNKNOWN、qa CONFIRMED），run 留在 SUBMIT 未进 IPIPE；reconcile 读回 CR 123064552 后两仓 CONFIRMED、SUBMITTED，进入 IPIPE，qa 未重推。覆盖 MVP 场景 S4、S6（claim 后、receipt 前中断）与 S7 的 UNKNOWN→CONFIRMED。

## 2026-10-31 · 发布段：查询异常不再崩溃；测试分支提测跳过约定

- `branch_release_build`：BranchPipeline 构建查询遇到网关拒绝（实测一次 `PERMISSION_DENIED`，重试即恢复）时返回 TRANSIENT 停车，不再抛出未捕获异常中断 worker。
- `tom-project-bgw/references/runtime-topology.md`：bgwagent BranchPipeline 348141 的测试准出为人工确认，跳过不算准出，发布会被拒（“没有执行准出，不能发布”），测试分支上需点确认。
- `tom-project-bgw/references/runtime-topology.md`：记录测试分支的约定——提测由需求负责人在平台点跳过，准出审核按自身表达式跳过，测试准出无需 QA 点击；控制面不自行跳过提测。BGW-2019 第一轮发布段发现。

## 2026-10-31 · 审批通知类写入不再阻断 run

- `recovery.NOTICE_OPERATIONS`（`approval.resume_notice` / `progress_notice` / `ack` / `nudge` / `ide_turn_notice`）：这些如流消息只是通知，不决定也不投递门。其未回执的 intent 不再让 `resume` 返回 QUERY_REQUIRED、不再挡阶段推进（`_blocking_pending`），状态摘要也不再列为未确认写入。审批投递（`approval.delivery.*`）照旧阻断。
- 起因：BGW-2019 第二个 run（S1）在 SUBMIT 卡成 RECOVERY_REQUIRED，原因是如流网关拒收了一条“已批准、请继续”通知（`MESSAGE_REJECTED:GATEWAY`），对应 intent 没有回执。

## 2026-10-30 · 状态摘要：v2 旁路写入不再显示为阻断

- `run_brief`：evidence contract v2 的 run 中，未回执的 KU/iCafe 写入（`ku.*`、`icafe.*`、`knowledge.*`）改列为“延后补发的旁路写入（不阻断）”，不再写成“未确认的外部写入”，下一步也不再要求先收敛它们（`recovery.resume` 本就跳过）。BGW-2019 发布段发现：摘要一直提示先收敛 KU 写入，实际 G9 已批准待提交。

## 2026-10-30 · 合同 3.4：RESTART lineage

- 新增 `restart-from SOURCE`（默认只读列出遗留远程对象与处置；`--execute --reason` 才提交）。同一 SQLite 事务内 CAS 源 run 检查点、未停则写 STOPPED、插 `run_lineage`、建目标 run 的 INTAKE；一个源只能重开一次，重复执行回放。
- 阻断（`RESTART_SOURCE_UNRESOLVED`）：无回执或 UNKNOWN/AMBIGUOUS 的写（v2 下 KU/iCafe 旁路除外）；未决处置的 CR（开启中需 `--keep-open-crs` 记 KEPT_FOR_REFERENCE，已合入需 `--accept-merged`，状态查不清记 ABANDONED_UNKNOWN）；运行中或读不到状态的 build；有未提交改动的源工作树。已 RELEASE_SUCCESS 的源不可重开。
- 目标 run 从 INTAKE 重新走 D 段；源的设计产物只作引用记入 lineage；已确认的修复轮次随 INTAKE 继承（`inherited_repair_rounds`，修复预算一并计入），环境重试不继承。
- `Orchestrator.start` 拆出 `_intake_event`，供 restart 在自己的事务里写 INTAKE。
- CR 状态用 `icode-cli api get_review_info -n`（逐个精确查询；仓库列表按状态分页 20 条，翻不到的 CR 会被误判为不存在）。

## 2026-10-30 · 测试卡预授权：无人值守推进

- 新增 `preauthorization.py`：本机 `~/.tom-autodev/preauthorizations.json` 里的 grant（卡号、项目、门号、允许分支、代答人、到期时间）覆盖的门，在双渠道投递完成后由代答人身份经台账 APPROVE，并写审计 `approval.preauthorized:{run}:{approval_id}`。profile 任一分支不在允许列表、过期、门号不符、代答人不是成员、投递不全时都不代答。
- `process` / `continue` / `drive` / `submit-draft` 在被代答的门后同一命令内自动续跑（上限 12 次）。
- 起因：BGW-2019 是不上线的测试卡，每个门等人点击，夜间和周末无法推进。`references/approval-policy.md` 同步，真实需求不得配置 grant。

## 2026-10-30 · MVP 验收口径：按独立 run 计数

- `docs/MVP_CONVERGENCE_PLAN.md`：场景矩阵 S1~S7 与"至少 3 个到 RELEASE_SUCCESS"改为按独立 run 计数，可复用测试卡 BGW-2019 及其专用分支（上一个 run 终态后 `start --fresh`）；每个 run 须有自己的改动、CR 与 build；GRILL/SPEC 对不同需求类型的覆盖在 P 层记"未验证"；受控制造故障前须先向需求方说明中断点与遗留远程对象并获确认。

## 2026-10-30 · 用例失败证据入账

- 作业退出码为 0、但日志报告失败用例（`JOB_SUCCEEDED_WITH_FAILED_CASES`）时，监控结果此前是 FAILURE 却只有 SUCCESS 的 stage、且没有失败签名，入账被 schema 拒绝（`SCHEMA_INVALID`）。现在把日志报告失败的 job 及其 stage 记为 FAIL，并生成按失败用例名计算的签名（顺序无关，失败集合不同即不同根因）。
- `ipipe_logs` 单个日志读取上限由 8MB 提到 64MB：bgwagent `p0级自动化回归测试` 的作业日志约 34MB，超限后读不到用例结果。
- 基线对比：作业日志里失败的用例，若在本 run 工作树基线修订（`worktree_ownership.baseline_revision`）的同流水线、同 stage 配置、同名 job 中也失败，视为既有失败，不再判本次改动失败；剩余全为既有失败时该 job 按通过处理，并写审计记录 `ipipe.case-baseline-accepted:{run}:{job}`。只剩新增失败时，`case_failures` 只列新增用例，另带 `preexisting_cases` 与 `baseline_build_id`。找不到可比基线（无该修订的构建、基线即候选修订、同仓多个基线）时，所有失败用例照旧计入。基线结果按 run 落检查点，大日志只读一次。
- 同一 stage 经 G8 重跑后又以完全相同的方式结束时，下一次重跑的绑定与已消费的审批同哈希，永远无法再批。此时绑定加入已成功派发的历次尝试（`prior_attempts`），新 G8 得到新哈希；参数或证据不同的绑定保持原样。
- BGW-2019 的 bgwagent p0 回归发现：68 个失败中 67 个与基线 build 270132995 相同，仅 `test_dsr_off_tcp_cfg_vs_006` 为新增（日志 `Connection reset by peer`）。

## 2026-10-29 · changelog 每次改动必须带新版本号

- 规则（`references/changelog-policy.md`）：任何仓库、任何格式，每个 change-set 都要新增版本；Keep a Changelog 的 `CHANGELOG.md` 不再只写 `[Unreleased]`，而是在其下方插入新版本小节（如 `0.1.1 - <日期>`），`[Unreleased]` 模板保持不动；文档/测试改动按 PATCH 递增。
- `prepare-change-set` 强制：根目录 change log 首个版本标题必须大于基线，否则 `CHANGELOG_VERSION_NOT_BUMPED:<role>`。tom-implement / tom-review 同步。
- 起因：BGW-2019 的 qa 仓 CR 只在 `[Unreleased]` 下加了条目，没有版本号。

## 2026-10-29 · BGW-2019 真实运行中修的问题

- IPIPE 证据入账：经 `route=IPIPE` 回到流水线时，同一 build 的新结果按回归事件单独入账（`ipipe-ingest:{run}:{build}:resume:{event}`），不再与上一次失败的入账冲突（`COMPLETION_CONFLICT`）。
- 参数映射补 `get_bgw` → `baidu/sysip/x86bgw`（bgwagent ChangePipeline 348142 的 `p0级自动化回归测试` 用它下载并启动被测 BGW）。
- 诊断新增 `route=IPIPE`：环境类失败已在代码外解决、且失败 stage 已在失败之后经 G8 重跑时，DIAGNOSE→IPIPE 回到流水线继续其余模块（沿用 IPIPE 入口绑定），不产生业务补丁。非环境类不得使用；没有对应 G8 重跑回执时停在 `IPIPE_RESUME_WITHOUT_RERUN`。tom-diagnose 合同同步。BGW-2019 P0新case回归 处理中补上。
- `P0新case回归` 的 `get_bgw_test_case`（上游自动触发时用了配置里写死的旧 qa 产物 node 102205340，404）：与 `get_bgwagent` 同一类问题。改为（1）准备人工 stage 前只读采纳所有已变更产物模块的 build（含 QA ChangePipeline 504074）；（2）下游 stage 继承的、且本 run 已在同一 build 的上游 stage 经 G8 填过的参数（`get_bgwagent`←`p0级自动化回归测试`）不再算作 job 覆盖，stage 只填自己声明的 `get_bgw_test_case`。未经本 run 上游填写的继承参数仍按 `JOB_PARAMETER_OVERRIDE_UNSUPPORTED` 阻断。
- IPIPE 多模块：x86bgw 的人工 stage（p0 回归，参数 `get_bgwagent`）需要 bgwagent 的产物，但逐模块推进时 bgwagent 的 build 尚未被本 run 采纳，产物查找报 `PRODUCT_URL_NOT_PUBLISHED`。准备人工 stage 的 G8 前，先只读采纳其余必需模块在冻结版本上的 build（不触发、不审批）。
- 提交阶段：已通过评审、但因兄弟任务修复离开 SUBMIT 而没建描述符的任务，仍计入待提交（`_latest_unsubmitted_reviewed_task` / `_submit_followup_state`），不再把 run 送回无事可做的 WORKSPACE（`WORKSPACE_TASK_REPO_UNRESOLVED`）；已误入的 WORKSPACE 由 `_recover_skipped_submit` 拉回 SUBMIT。未改动的仓库 HEAD 停在他人基线提交时，`_commit_if_dirty(baseline=...)` 不再判 `GIT_IDENTITY_MISMATCH`。
- 同一 run 内 CODE_ONLY 修复已提交的任务：iCode 对账把本卡、同 owner、同分支、同 Change-Id 的已开 CR 识别为 `CR_APPEND_ALLOWED`（作为新 patchset 追加），不再判为 `CR_IDENTITY_CONFLICT`。G7 卡说明该追加情形；控制器门（G7/G8）的审批预览文件改为展示固定摘要，不再误写“材料缺失”。BGW-2019 修 qa 仓版本号时发现。
- 仓库未配置 git 提交身份时：`_commit_if_dirty` 由未捕获的 `CalledProcessError` 改为返回 `GIT_IDENTITY_REQUIRED`；`WorkspaceManager.create` 在建工作树（G4 之前）就检查，避免计划批完后才在实现阶段失败。BGW-2019 的 bgwagent 仓发现。
- 多任务 run 的第二个任务进入 WORKSPACE 时 G4 卡材料缺失（`APPROVAL_MATERIAL_MISSING`）：`approval_summary._workspace_context` 读取了全 run 的 ACTIVE 工作树，先命中上一任务的行。改为按 `task_id` 过滤（另两处查询按 `(task_id, repo_path)` 取值，本就正确）。
- PLAN 草稿校验失败时说明原因：`G4_INPUT_MISMATCH` 带 `expected_g4_input_hash`（应抄 PLAN action 自己的 `input_hash`，不是 WORKSPACE 的 G4 审批 hash）；`TASK_PLAN_MISMATCH` 带 `mismatched_fields` 与 DAG 节点的期望值。tom-plan SKILL.md 同步澄清。此前两处只返回原因码，生产方只能逐项猜。

## 2026-10-29 · 工作流与 code agent 无关；Stop hook 只续跑驱动会话

- 新增 `agent_host.py`：识别当前 agent（`TOM_AUTODEV_AGENT`，或 Claude Code / Codex / Comate 各自导出的环境变量），按 agent 顺序查找已安装 skill（iCode、ku-doc-manage、infoflow-message-group），可用 `TOM_AUTODEV_<SKILL>_SKILL_DIR` 覆盖。
- 去掉写死的 `~/.comate/skills/.system/icode` 默认值（`submit` / `submit-companion` / worker），KU 与预检的如流脚本同样跨 agent 解析；`install_links` 默认安装到 Comate、Claude Code、Codex 三个 skill 根目录；预检识别三家的 Stop hook 配置。
- 修复：Stop hook 会把"审批已通过，继续执行"注入到无关会话（例如在调研别的问题的 Claude 会话）。现在 `process` / `continue` / `drive` / `submit-draft` 记录驱动会话 id，hook 只对该会话自动续跑，其他会话返回 `NOT_DRIVER_SESSION`，只收如流提醒。
- 用户可见提示由"Comate"改为"agent / 驱动本 run 的 agent 会话"；SKILL.md、tom-grill、README 去掉"仅 Comate"表述。信封中的 `host` 字段仍为 `comate`（表示控制面合同版本，不表示具体 agent，改动会使历史 artifact 失效）。
- 测试：新增"非驱动会话不续跑"；全量 1587 通过。

## 2026-10-29 · changelog 规则覆盖所有改动仓库

- `references/changelog-policy.md`：每个被改动的仓库（业务仓与产品测试仓、代码与纯文档）都要在同一 change-set 内写根目录 change log；补充 Keep a Changelog（`CHANGELOG.md` 的 `[Unreleased]`）格式。
- `prepare-change-set` 强制：仓库有改动但根目录 `changelog` / `CHANGELOG.md` 等未改，返回 `CHANGELOG_ENTRY_MISSING:<role>`。
- tom-implement 第 7 步、tom-review 核对项同步。测试：`test_prepare_changelog_guard.py` 5 项。

## 2026-10-29 · MVP §7：LegacyRunProjector

- 新增 `legacy_projector.py`：`status` / `context` 只读 events、artifacts、approval ledger，不调 live workflow spec、`next()`、migration、repin 或远端；`drill` 对库内全部 run 投影并与 `run_brief` 对照。
- 本机演练：13 个历史 run（BGW-1956 / 1995 / 2019）13 个一致（state 相同，live 待批门 ⊆ 投影待批门）。
- 删除旧模块的其他前提（真实场景矩阵、无非终态旧 run 等）仍未满足，本次不删任何代码。
- 测试：`test_legacy_projector.py` 2 项。

## 2026-10-29 · MVP 评审修复第 8 轮（Codex 1 条 P1、1 条 P2）· 评审收口

- 多仓 PARTIAL 只在有 AMBIGUOUS 仓库时转 DIAGNOSE；仅被平台拒绝（REFUSED）的保留在 SUBMIT，修好外部原因后同一 attempt 重试（拒绝回执被 supersede、重试链式接续，已确认的兄弟仓不重推）。
- trigger 在 POST 前记 `ipipe.trigger.dispatch`；重放时只有确实派发过才按创建时间过滤，预检失败（未派发）的重放仍可只读采纳既有 build。新代码 claim 时记 `ipipe.trigger.claim`，旧 intent 无标记按"已派发"保守处理。
- 评审收口：GPT-6.1-Sol 连续两轮（6、7）未发现问题；Codex 第 8 轮只剩恢复路径细节。剩余并发/中断交错改由 BGW-2019 真实运行验证，不再开新一轮静态评审。
- 测试：新增 2 项；全量 1579 通过。

## 2026-10-28 · MVP 评审修复第 7 轮（Codex 2 条 P1、1 条 P2；GPT-6.1-Sol 连续两轮未发现问题）

- iCode 目标级围栏：push 前若同一 run 中同仓库同分支还有未决（CLAIMED/UNKNOWN/AMBIGUOUS）的其他提交，返回 `TARGET_INTENT_UNRESOLVED` 不推；PARTIAL 诊断豁免只放行诊断，不放行新的写。
- dispatch 围栏窗口改为完整的缺席宽限期（push 超时 + 列表延迟），从 dispatch 起算。
- trigger 重放时先看已记录的只读采纳，再做 discovery；新出现的 build 不会顶替它。
- 测试：新增 3 项；全量 1577 通过。

## 2026-10-28 · MVP 评审修复第 6 轮（Codex 3 条 P1；GPT-6.1-Sol 未发现 P0/P1/P2）

- iCode 写入围栏：push 前记 `icode.submit.dispatch` 观察并复查该 intent 是否已被关闭（已关闭返回 `SUBMIT_INTENT_CLOSED`，不 push）；`CONFIRMED_ABSENT` 写入与"push 超时窗口内的 dispatch 标记"在同一事务判定，标记存在时只记 UNKNOWN。两个顺序都不会出现第二次 push。
- v2 发布遇控制面守卫（`WORKFLOW_SPEC_DRIFT`、`PROFILE_*`）原样返回，不转成延后、不固定决定。
- trigger 全新 claim 采纳既有 build 时记 `ipipe.trigger.adopt`；确认读失败后的重放按该记录找回 build，不被创建时间过滤掉，也不发 POST。
- 测试：新增 4 项；全量通过。

## 2026-10-28 · MVP 评审修复第 5 轮（Codex 1 条 P1、3 条 P2 + GPT-6.1-Sol 1 条 P1、1 条 P2，P1 为同一问题）

- iPipe rerun / rebuild / trigger 三条成功回执改走 `receipt_unless_ambiguous`，并发对账已记 AMBIGUOUS 时不再被成功回执覆盖（返回 `INTENT_AMBIGUOUS`）。
- rerun 派发抛出超时/瞬时错误后的对账改走 `_rerun_verdict` + `_settled_write`，AMBIGUOUS 结论会落盘并保持粘滞。
- iCode 首次预对账发现身份冲突时记为 AMBIGUOUS（原样返回 `CR_IDENTITY_CONFLICT` 改为 `CR_IDENTITY_AMBIGUOUS`），多仓聚合即为 PARTIAL、可转诊断。
- scope 台账接受补发的 `SCOPE:<名>#retry-N` 卡展开（按 `gate_of` 归一）。
- v2 发布决定写入冲突时读回先写者的结论，并发完成不再抛 `ValueError`。
- 记账：`ipipe-rebuild` 执行与对账入口尚未加 run lease（原子回执已关闭其主要后果）。
- 测试：新增 3 项；全量 1570 通过。

## 2026-10-28 · MVP 评审修复第 4 轮（Codex 4 条 P1、1 条 P2 + GPT-6.1-Sol 1 条 P1、2 条 P2）

- iCode 成功回执与缺席回执都经 `receipt_unless_ambiguous` 写入：已有 AMBIGUOUS 时不写，已有回执时返回已落的那份（先到的终态结论为准，不再 `RECEIPT_CONFLICT`）。
- iCode NEW 列表缺 `data.changes`（错误信封）为 `CR_RESPONSE_INVALID`，不能证明缺席。
- trigger 归属读取嵌套的 `trigger.triggerTime`（RECORDED fixture 的实际结构），其次顶层字段与 `startTime`。
- 合同 3.3：失败 stage 重跑的 G8 也绑定实时配置 hash（同 build、同 stage id，配置是唯一会漂的部分）。
- rerun / rebuild / trigger 的对账遇到 profile 守卫拒绝返回 REFUSED，不写 `intent.state`。
- `submit_to_ipipe` 的仓库集合校验移到第一次 push 之前。
- `SUBMIT_PARTIAL` 带入 DIAGNOSE 的未决 intent 在 run 离开 SUBMIT 期间持续豁免（`recovery.partial_held`，`PhaseProtocol._blocking_pending` 同样适用），回到 SUBMIT 后恢复阻断；intent 本身始终不关闭。
- 测试：新增 7 项；全量 1559 通过。

## 2026-10-28 · MVP 评审修复第 3 轮（Codex gpt-6-astra 4 条 P1 + GPT-6.1-Sol 2 条 P1、1 条 P2）

- iCode：`claim` 为 EXISTING 的提交一律只对账，`:after:` 后继 intent 不再因祖先已缺席/放弃而再次 push。
- `settle` 写 `CONFIRMED_ABSENT` 与 AMBIGUOUS 检查在同一事务（`StateStore.receipt_unless_ambiguous`）；并发对账已记 AMBIGUOUS 时陈旧的缺席结论被丢弃。
- v2：发布成功的回执也按 artifact 固定（与延后同一个 key），重放时 KU 故障不会把已发布的 envelope 改成延后；`Recovery.resume` 同样豁免 v2 的 KU/iCafe intent，WORKSPACE→PLAN 等控制器迁移不再被它们卡住。
- 合同 3.2：`submit_to_ipipe` 自己从获批 descriptor 与 worktree ownership 推导必须提交的仓库，调用方只能加不能减（缺漏返回 `SUBMISSION_SET_MISMATCH`）；`route_failure("SUBMIT_PARTIAL")` 只对该 PARTIAL 证据里列出的 intent 放行进入 DIAGNOSE，intent 本身不关闭、继续阻断同目标写。
- 测试：新增 4 项（真实 `route_failure`/`Recovery` 路径）；全量 1552 通过。
- 修复迭代 3 次已用完，未再发起评审。

## 2026-10-28 · MVP 评审修复第 2 轮（Codex gpt-6-astra + comatecli GPT-6.1-Sol；opus 5.5 本轮无新问题）

- iCode：`CONFIRMED_ABSENT` 宽限期改为 push_cr 超时（600 s）+ 300 s，push 仍可能在进行时并发 reconcile 不会判缺席后重推（Codex P1）。
- 证据合同 v2：同一 artifact 的延后决定持久化（`knowledge.deferred:{run}:{hash}:{scope}`），完成中途中断后重试不会因 KU 恢复得到不同 envelope 而 `ARTIFACT_CONFLICT`；v2 run 中所有 KU/iCafe intent（含 `icafe.comment.create`）都不再阻塞 `RECOVERY_REQUIRED`（Codex P1 ×2）。
- 合同 3.2：`submit_to_ipipe` 自身在归档 submission、迁入 IPIPE 前按完整 attempt 求聚合，非 SUBMITTED 返回 `SUBMISSION_SET_INCOMPLETE`；PARTIAL 结果经 `route_failure("SUBMIT_PARTIAL")` 幂等转 DIAGNOSE（GPT P1 + P2）。
- trigger 归属改为正向时间证明：优先 `triggerTime`（创建时间），否则 `startTime`；无可用时间或早于 intent 创建的（含"早创建、晚启动"的排队 build）一律不采纳，保持 UNKNOWN（GPT P1）。平台时钟对齐仍用本地 intent 时间 ±120 s，记账。
- 测试：新增 7 项；全量 1549 通过。

## 2026-10-28 · MVP 评审修复第 1 轮（comatecli；Codex 两次因网关 403 未产出）

- P1 iCode 新 CR 缺席：`_submit_verdict` 判 `CONFIRMED_ABSENT` 前除 NEW 外还查 MERGED / ABANDONED；有已关闭 CR 携带本 commit 或 Change-Id 判 AMBIGUOUS（不再重推），任一列表失败或未取完保持 UNKNOWN。
- iPipe trigger 确认只认 `startTime` 晚于 intent（120 s 偏差）的 build；全新 claim 仍可采纳既有 build（不发写）。
- 合同 3.2 聚合：任一仓 AMBIGUOUS / 拒绝即 PARTIAL（不再要求先有已确认的兄弟仓）；已确认但缺 CR 号为 PARTIAL + `RECEIPT_MISSING_CHANGE_NUMBER`；按 `live_idempotency_key` 取 live intent；SUBMITTED 记录冲突返回 `SUBMISSION_CONFLICT` 而非抛异常。
- `intent_state`：ok False 且无状态标记的回执派生为 `REFUSED`，不再与 CONFIRMED 混用（仍不阻塞目标）。
- scope 台账：成员策略须与门一一对齐（`SCOPE_POLICIES_MISALIGNED`）；`scope_hash` 对门列表排序，顺序不影响幂等；展开时遇到成员更窄的待批逐门卡拒绝（`SCOPE_GATE_POLICY_NARROWER`）。
- 合同 3.3：`config_snapshot` 纳入 `upStageParam` / `defaultValue`、job 组顺序与每个 job 的 shell 命令 hash；G8 卡对"实时配置已删除此 stage"给出提示。
- watcher 指纹覆盖 `schemas/*.json`。
- 未改（记账）：stage `kind`（QA 只等待、发布只由 R' 消费）随 R 卡接入 worker 一起做；artifact_store 的 deferred 旁路只在 v2 run 产生，v1 不可达；trigger 10 分钟 ABSENT 窗口未加，未确认时保持 UNKNOWN（偏保守）。
- 测试：新增 8 项；全量 1543 通过。

## 2026-10-28 · MVP §2：scope 授权（台账层）

- `ApprovalLedger.request_scope`：一张卡代表多个门，卡的 input hash = `scope_hash`（`scope-approval.v1`：scope 名、按序的 (gate, input_hash) 列表、规则 hash）；有效审批人取各门成员策略的交集，交集为空时拒绝出卡（`SCOPE_MEMBERS_EMPTY`），退回逐门审批。
- `ApprovalLedger.expand_scope`：卡被批准后，为列出的每个门写一条派生同意（各自的 gate 与 input_hash，`effective_channel = scope:<approval_id>`），现有的门检查无需改动即可识别。防重放：artifact 或规则变了（scope hash 不匹配）一条都不写；重复展开幂等；已被停止 / 超时 / 需修改的门不会被覆盖（`SCOPE_GATE_ALREADY_DECIDED`）。
- 尚未接入 worker：D/C/R 卡需要 prepare 段（中间门不再逐个找人批）改门的语义，按方案应在合同 3.1~3.3 经真实 run 验证后再接，见方案第 8 节。
- 测试：`test_scope_approval.py` 6 项；全量通过。

## 2026-10-28 · MVP 合同 3.3：stage plan 快照绑定进人工 stage 的 G8

- 新增 `stage_plan.py`（`stage-plan.v1`）：build 自身的 stage 形状（id、配置 id、名称、顺序）和实时配置中 stage 运行依赖的部分（stage 身份、上游、`paramBeans` 名称与 required、`inParamsMap` 键、job 的 id 与类型）分别规范化后 sha256。无关字段（修改人、时间）不进 hash。
- 人工 stage 的 G8 binding 新增 `stage_plan: {build_hash, config_hash}`；执行前重新读取，两次之间配置被改则 hash 不匹配，执行被拒，需新 G8。每个 stage 的 G8 仍单独消费（已有的 approval-id 维度 rerun key 即 nonce）。
- 配置里已删除、但 build 快照仍带着的 stage：以 build 自身的显式空声明为准（`configuration_source: build_snapshot`、`config_stage_missing: true`），不再报 `STAGE_PARAMETERS_UNKNOWN`；非显式空声明仍是未知（BGW-1995「系统测试功能回归」的场景）。
- 测试：stage plan 4 项、配置删 stage 的回放测试改为新行为；全量 1529 通过。

## 2026-10-28 · MVP 合同 3.2：多仓提交聚合状态

- 新增 `submission_set.py`：一次提交 attempt =（change_set_id, revision_set_id），按各仓 iCode intent 的持久状态推导聚合状态：`SUBMITTED`（每仓都 CONFIRMED 且有 CR 号，只记录一次、只写一次）/ `PARTIAL`（有仓已确认，另一仓 AMBIGUOUS 或被拒）/ `PENDING`（未决或未尝试）。重试沿 `:after:` 链，取每仓最新的 intent。
- `_submit` 每次返回附带 `submission_set`。进入 IPIPE 的顺序保证不变：测试库先推，任一仓未成功就停在 SUBMIT；主仓推送确认后才在 `submit_to_ipipe` 里迁移。PARTIAL 只报告、不推进，由 F 决定。
- 不可变 attempt：改代码产生新 change set 即新 attempt，旧 attempt 记录不改；旧 build 的 `SUPERSEDED` 沿用现有排除集。
- 测试：`test_submission_set.py` 6 项；全量通过。

## 2026-10-28 · MVP §5：证据合同 v2（KU / iCafe 降为旁路，按 run 双读）

- 新 run 的 INTAKE 固定 `evidence_contract: "v2"`，G0 hash 升到 `intake_hash_version: v3`（多绑定这一字段）；v1 / v2 的旧 INTAKE 照原样计算 hash，旧 run 不受影响。
- v2 下 `PhaseProtocol._publish`：KU/iCafe 写入失败、抛异常或回读未确认时，换成 deferred receipt（`knowledge-deferred` 证据引用，KU/iCafe 字段为空），记日志后照常迁移状态；`_receipt_error` 接受同一 run、同一 artifact hash 的 deferred receipt；外来的未决 publish intent 不再阻断 v2 run。`ArtifactStore` 接受带 `knowledge-deferred` 且 KU/iCafe 字段全空的信封。completion predicate 的 `requires_ku_receipt` / `requires_icafe_receipt` 随合同版本变化。
- v1 run 的行为不变：`test_continue_reconciles_g5_saved_draft_through_real_ku_client` 改为在 v1 run 上跑，仍要求 KU receipt；新增 v2 下 KU 不可达时照常推进的 e2e。
- `references/phase-protocol.md` 的完成条件按合同版本改写。deferred 的文档由 run 结束后的 `backfill_knowledge` 归档（现有命令）。
- 全量 1519 通过。

## 2026-10-28 · MVP 合同 3.1 完成：BranchPipeline 发布身份

- 「发布」stage 的执行本身已经是人工 stage 执行（走 IntentExecutor）。发布身份读取 `_branch_release` 改为收集全部匹配的 AGILE_RELEASE job：同一 revision + branch 有两个以上 release → `RELEASE_AMBIGUOUS`（带 `candidates`），不取第一个；worker 遇到它按失败停下，不写 RELEASE_SUCCESS。
- 合同 3.1 的 6 种写操作全部接入状态机。

## 2026-10-28 · MVP 合同 3.1：iCode 新 CR 与追加 patchset 迁入 IntentExecutor

- 身份确认（`IcodeRuntime._submit_verdict`）：新 CR 按 commit sha / Change-Id 找到的确切 CR（目标分支、owner、卡片标题都对）→ CONFIRMED；追加 patchset 按 CR 号 + 当前 patchset sha → CONFIRMED。
- AMBIGUOUS（`CR_IDENTITY_AMBIGUOUS`，带 `candidates`，之后保持不变）：同一 commit / Change-Id 对应的 CR 不是确切那一个（例如两个 CR）；追加时目标 CR 在 precheck 之后被别人改成了另一个 patchset。原来这两种分别报 `CR_IDENTITY_CONFLICT` / `SUBMIT_CONFIRMATION_REQUIRED`，之后再提交返回 `QUERY_REQUIRED`，无法区分。
- CONFIRMED_ABSENT（`SUBMIT_CONFIRMED_ABSENT`，`retry_allowed: true`）：仅在 claim 后 5 分钟以上、且 NEW 列表完整（`hasMore` 不为 true）时：新 CR 的 commit 在列表里不存在，或追加目标仍停在 precheck 记录的 patchset。结果写成 receipt，下一次提交按已有的 `:after:` 链开新 intent，可以再 push 一次。push 之后的即时确认不会判 ABSENT。
- push 成功但 CR 列表滞后时，改为最多 3 次只读确认（间隔 5 秒），不再直接报 `SUBMIT_CONFIRMATION_REQUIRED`。
- 新增 `Verdict.refused`：profile 漂移等守卫拦下确认时，原样返回守卫结果，不写任何状态记录（保持 `test_icode_profile_guard` 的"漂移时不写入"）。
- `intent_executor.answer` 统一把 verdict 转成调用方结果，iPipe 和 iCode 共用；`_superseded_external_receipt` 把 CONFIRMED_ABSENT 视同可接续的已关闭尝试。
- 测试：iCode 新增 5 项，替换 1 项；全量 1517 通过。

## 2026-10-28 · MVP 合同 3.1：iPipe trigger 与 rebuild 迁入 IntentExecutor

- trigger：回执不带 build id 时不再直接报 `TRIGGER_RESPONSE_INVALID`，改为按冻结 revision 有限次只读回查（`_trigger_verdict`）；恰好一个 build → CONFIRMED，两个以上 → AMBIGUOUS（`BUILD_AMBIGUOUS`，带 `candidates`），都没有 → 保持 UNKNOWN。超时/异常后同样走这个判定。已经 AMBIGUOUS 的 trigger intent 重放时直接返回记录的候选，不采纳任何一个、不再 POST。
- rebuild：`_reconcile_rebuild` 的结果统一经 `_rebuild_verdict` 落成状态；无 build id 的回执改为有限次只读确认；前一次 rebuild 处于 AMBIGUOUS 时拒绝新 attempt。
- 对外 reason code 保持不变（`BUILD_AMBIGUOUS`、`TRIGGER_RESULT_UNKNOWN`、`REBUILD_RESPONSE_INVALID`、`QUERY_REQUIRED`），新增 `intent_state` 字段；`discover` 的 `BUILD_AMBIGUOUS` 现在带候选列表。
- `intent_executor.sticky_ambiguous`：AMBIGUOUS 只记录一次，后续读取沿用第一次的证据。
- 测试：`test_intent_executor.py` 新增 trigger 4 项；全量 1513 通过。

## 2026-10-28 · MVP 合同 3.1：IntentExecutor 状态机，先迁 iPipe 人工 stage 执行

- 新增 `intent_executor.py`：外部写的状态 `CLAIMED / UNKNOWN / CONFIRMED / CONFIRMED_ABSENT / AMBIGUOUS / ABANDONED_LOCAL / ABANDONED`，从已有的 receipt 和 `intent.state` observation 推导，不加表、不改历史数据。`CONFIRMED_ABSENT` 写成 receipt（释放目标，可重试）；`UNKNOWN` / `AMBIGUOUS` 只写 observation，目标继续被 `claim_exclusive_intent` 的前缀互斥挡住。`AMBIGUOUS` 对自动化是终态，reconcile 不会把它读成别的状态。
- iPipe 人工 stage 执行 / 失败重跑接入：无身份回执的有限次只读确认改为 `intent_executor.confirm`；`reconcile_rerun` 和重放路径走同一个 `_rerun_verdict`。新判定：同一 stage 有两个以上未被认领、且都在 intent 之后开始的 successor → `INTENT_AMBIGUOUS`，不再取第一个；已有 AMBIGUOUS 的 stage 拒绝新 G8 写入。人工 stage 不会被自动判 ABSENT（请求可能还在排队），仍需人工 `abandon-intent`。
- 返回结果新增 `intent_state` 字段；原有 reason code（`RERUN_RESPONSE_INVALID`、`QUERY_REQUIRED`、`RERUN_NOT_STARTED_UNPROVEN`）不变，旧 run 不受影响。
- DERIVED fixture 2 份：无身份回执（来源：BGW-1995 五次真实执行的 observation `response_keys [code, entities, msg]`）、回归未完时「发布」显示 PENDING（由最终 stage 列表回退）。
- 测试：新增 `test_intent_executor.py` 13 项；全量 1509 通过。

## 2026-10-28 · MVP 第 1 步：watcher 感知代码版本、真实平台 fixture

- watcher 感知代码版本（`code_version.py`）：`watch-approvals` / `watch-ipipe` 启动时记下控制面源码（非测试 `.py`）的内容 hash，每轮比对，变了就以 `CODE_VERSION_CHANGED` 正常退出，由 `supervise.sh` 拉起新代码。起因：旧 watcher 跑旧代码，10-25 误报 8.4 万条 `WORKFLOW_SPEC_DRIFT`，10-27 再次发生。
- 真实平台 fixture（`tests/fixtures/platform/`，`platform-fixture.v1`）：`record.py` 只读录制 BGW-1995 的 12 份 iPipe 响应，按端点白名单只留结构和身份字段，不含人员、主机、日志链接和凭据（仓库是公开的）；每份带 `content_sha256`、`origin`、来源对象。`tests/platform_fixtures.py` 回放并校验 hash。
- `test_platform_fixtures.py` 用这些真实响应验证：表达式跳过（只有整 stage 全 EXPRESSION 才算，单测、p0级自动化回归测试这类部分跳过的仍需通过）、参数在配置 `paramBeans` 而 `manualParams=null`、配置中已删除的 stage（当前 fail-closed，合同 3.3 将改）、发布身份来自 `AGILE_RELEASE` job、`getReleaseInfo` 404。
- 不改状态机、审批语义和 workflow spec hash。全量 1496 通过。

## 2026-10-27 · 早到的人工阶段 G8 在执行层失效

- 上一条只修了 worker 的选择；已批出去的错误 G8（c52c3b33…，「发布」）仍然有效：它的 input hash 只绑定 stage 本身，回归一跑完 worker 就会拿它执行发布。
- 修复：`IpipeRuntime._rerun_binding` 对人工 stage 先只读拉取实时 stage 列表，前面任何 stage 未完成就返回 `MANUAL_STAGE_NOT_READY`（不出 hash、不发 G8、不写意图）；就绪后把前序 stage 的状态写进 binding（`ready_after`），所以就绪前批的 G8 hash 不再匹配，执行被拒，需按就绪后的新 G8 重新审批。worker 遇到 `MANUAL_STAGE_NOT_READY` 时 park 继续等。
- 测试：新增 2 项（未就绪不出 G8；早批的 G8 不能执行、新 G8 可以）；全量 1480 通过。

## 2026-10-27 · BranchPipeline 只推进当前那个人工阶段

- 现象：BranchPipeline 275700446 的「自动化回归测试」还在跑时，「发布」已经显示 PENDING_FOR_USER；`_advance_branch_release` 取了 monitor 返回的第一个人工 stage，给「发布」发起了 G8（c52c3b33…），审批对象是错的。
- 修复：`worker_driver._next_ready_manual_stage` 只取第一个未完成的 stage，且它必须是等待人工的那个；前面还有 stage 在跑时 park 为 `RELEASE_WAITING`。错发的 G8 不批即可，超时后作废。
- 测试：新增 `test_branch_release_stage_order` 2 项；全量通过。

## 2026-10-27 · 人工阶段执行后多读几次确认，不再一律"结果不明"

- iPipe 人工阶段执行接口只回 `{code, entities, msg}`，不带 stage id；控制器立即读一次 stage 列表时开始时间还没更新，于是每次都报 `RERUN_RESPONSE_INVALID`/`CONTROLLER_STEP_FAILED`，需要人工 `--reconcile-only`（BGW-1995 在 348102 与 348101 上各发生一次）。`IpipeRuntime.rerun` 对无 stage 身份的确认改为最多 4 次只读确认、间隔 5 秒，仍从不重发写入。
- 测试：`test_ipipe_runtime` +1；全量 1476 项通过。

## 2026-10-27 · SUBMIT 同时提交测试库；发布改走 BranchPipeline

- 测试库提交丢失：任务同时改了业务库和测试库时，描述符 `revision_set` 记录两份 commit、G7 卡片也列出两个仓库，但 `_submit` 只推主仓库（业务库），测试库 commit 留在本地（BGW-1995 run 160c6840 的 qa `8c68089` 从未进 iCode，QA 504074 产不出新用例）。新增 `IcodeRuntime.submit_companion`（同一描述符、同一 G7 校验，推 revision_set 中非主仓库那一份）和 `orchestrator._companion_binding/_submit_companion`，`_submit` 先推测试库再推业务库并转 IPIPE；回执存为 `companion-submission`。已越过 SUBMIT 的旧 run 用 `submit-companion RUN_ID --task T` 补提，不改 run 状态。
- 发布核验：`/api/agile/getReleaseInfo` 在真实网关一律 404。`verify_release` 先查目标分支 BranchPipeline 对本次 revision 的 build，读其 `AGILE_RELEASE` job 的 version/revision/branch/产物 URL 作为发布证据；查不到 BranchPipeline 的适配器回退旧接口。未发布时返回 `RELEASE_WAITING` 并带上 branch build。
- 发布推进：`worker_driver._advance_branch_release` 在 G9 后绑定该 BranchPipeline build，人工阶段（自动化回归测试/系统测试/提测/发布）复用 `_prepare_manual_stage`：参数按实时流水线配置发现、按冻结策略推导、每步 G8、结果不明只读对账。`stage_parameters` 把 BranchPipeline 的「自动化回归测试」登记为 `get_bgwagent` 的已知人工阶段。
- 文档：`tom-autodev/SKILL.md`、`tom-project-bgw/references/runtime-topology.md` 写明参数声明来自流水线配置而非 `manualParams`、`required:false` 不等于可为空、测试库提交与 BranchPipeline 发布路径（来自本次误判：曾把 `get_bgwagent` 必填阶段说成无参数）。
- 测试：`test_icode_runtime` +2、`test_ipipe_runtime` +2、`test_fake_e2e` 断言测试库同时提交；全量 1475 项通过。

## 2026-10-27 · iPipe：流水线表达式跳过的 stage 不算缺证据

- 现象：BGW-1995 run 160c6840 的 build 275535376 在 x86bgw ChangePipeline(348102) 上编译、单测、P0 回归全部通过，但 `P0新case回归`、`系统测试p0级功能回归` 被流水线跳过表达式对 dpdk-2011 分支固定跳过（`skipByStage: EXPRESSION`），平台整单报 `SKIPPED`；`monitor` 只认 SUCC 类整单状态且要求每个 stage 有运行 job，run 永远停在 MONITORING。
- 修复：`clients/ipipe_runtime.py` 新增 `_mark_expression_skips`（读 stage detail，全部 job 均为 `skipByStage=EXPRESSION` 的整段跳过 stage 标记为不适用）和 `_build_passed`（整单 SUCC 或 SKIPPED 时，排除表达式跳过 stage 后其余 stage 必须全部通过且至少一个实际运行）；`monitor` 与 `verify_release_of_build` 共用该判定。人工跳过、stage detail 读不到、或任一 job 不带该标记的跳过 stage 仍按缺证据处理；全部 stage 都被表达式跳过的 build 不算通过。
- 测试：`test_ipipe_runtime` 新增 3 项（表达式跳过通过、人工跳过仍阻断、全跳过不算通过）；全量 1471 项通过。

## 2026-10-25 · 依赖失败证据绑定 stage、只认 403/404、最新一次为准；结构化字段说明更正（B-21、B-22）

- B-23：同 stage/模块之后出现的 5xx 或缺状态码下载结果不再覆盖先前已证实的 403/404；只有成功下载才解除环境拦截（`environment_evidence.proven_dependency_failure`，测试 `test_b23_an_inconclusive_later_attempt_does_not_clear_a_proven_404`）。
- B-21（P2）：`environment_evidence` 重写。此前 `ipipe.dependency-failure:{run}:{build}:{module}` 只按 build 记录、不绑定失败
  stage，第一条永久有效，5xx 也记 `ENV_TRANSIENT`，`ipipe-rebuild` 探测 403/404 也写权威记录，`ipipe-rerun --print-parameters`
  只读检查也会写；结果是同一 build 上的真实代码失败被永久判为环境类、无法 REPAIR。现在：
  - 证据绑定 stage：键为 `ipipe.dependency-failure:{run}:{build}:{stage_build_id}:{module}:{attempt:04d}`，内容含
    `stage_build_id`、`stage_conf_id`、`module`、`declared_parameters`（该 stage 的参数声明）与 `consumed_by`（声明中映射到该模块的
    参数名）。`PhaseProtocol._authoritative_failure_class(run_id, diagnosed_stage)` 只读被诊断的失败 stage 的记录：诊断的
    `stage_id` 是 IPIPE 证据中的 FAILURE stage 时只看它；未给 stage 或给了未失败的 stage 时，逐个看证据里每个 FAILURE stage 自己的
    记录（不能靠改 `stage_id` 绕开）。`consumed_by` 为空、或 stage binding 的 build/`stage_conf_id` 与记录不一致时不算权威。
  - 只认 403/404：`class_for_statuses` 仅在全部候选为 403/404 时返回 `ENV_UNSATISFIED`；含 5xx 记 `advisory: ENV_TRANSIENT_SUSPECTED`，
    缺状态码或其他状态码记 `advisory: DOWNLOAD_STATUS_UNPROVEN`，只供诊断参考，不拦截。
  - `ipipe-rebuild --request-g8` 不再写依赖记录（删除 `record_probe_failure`）；探测结果只留在 G8 材料中，它只证明无法基于旧产物重跑。
  - 只读入口不写：`_stage_parameters` 新增 `record_evidence`（默认 False）；只有 `worker_driver._prepare_manual_stage`（写 G8 材料）
    与 CLI `ipipe-rerun --request-g8`/执行路径传 True，`--print-parameters`、`--print-input-hash` 与 rebuild 的 plan 步骤不写。
  - 不再"第一条永久"：选择"按尝试次数分键"。状态库的幂等结果不可覆盖，同 key 改写会破坏既有不可变约定；分键追加则每次尝试都留档
    可审计，读取时每个 stage/模块只取 attempt 最大的一条。一次下载成功也追加一条 `outcome: AVAILABLE`，覆盖早先的 404。
  - 旧格式记录（`{run}:{build}:{module}`）不再被读取，此前因它被拦截的诊断回到正常诊断 + G6。
- B-22（P3 文档）：README、`tom-autodev/references/failure-taxonomy.md` 与本文件对 `errorType`/`failType`/`failureType`/
  `systemErrorType`/`errorCategory`/`agentStatus`/`agentState`/`runnerStatus` 8 个结构化字段的描述改为：候选映射，未经真实 iPipe
  返回验证；当前观测到的响应均不含这些字段，因此实际不生效；首次在真实返回中观测到任一字段时须先核对语义再启用。代码映射保留。
- 文档：README、`failure-taxonomy.md`、`phase-protocol.md`、`tom-autodev/SKILL.md` 的依赖失败强证据描述同步为 stage 绑定且仅 403/404。
- 测试（修复前均失败）：`test_code_only_repair_baseline` 新增 `test_b21_another_stages_dependency_404_does_not_block_this_stages_code_repair`、
  `test_b21_a_5xx_is_advisory_and_never_blocks`、`test_b21_a_module_the_stage_does_not_consume_is_not_authoritative`、
  `test_b21_the_latest_attempt_wins`、`test_b21_a_diagnosis_cannot_dodge_by_naming_no_or_a_passing_stage`；
  `test_a_rebuild_probe_403_is_authoritative` 改为 `test_b21_a_rebuild_probe_403_is_not_authoritative`；
  `test_ipipe_rebuild` 的探测测试改为 `test_b21_a_requested_rebuild_probe_404_is_not_authoritative_environment_evidence`；
  `test_stage_parameters` 新增 `test_b21_read_only_resolution_writes_no_dependency_evidence`、
  `test_b21_cli_print_parameters_and_input_hash_write_no_dependency_evidence`。本 stage 消费 `get_bgwagent` 且全部 404 时仍拒绝
  REPAIR/SPEC/GRILL（`test_b11_every_exact_get_bgwagent_candidate_404_is_authoritative`、`test_a_proven_dependency_404_blocks_repair_and_spec`）。
  workflow spec hash 不变（`76bb964c…d410`）。

### 剩余风险（待负责人决定，未接受）

- `DIAGNOSE -> ENVIRONMENT_BLOCKED` 边未加：环境类诊断止于 STOPPED，只能绕行 `ipipe-rebuild` 或新开 run。重开条件：下次计划内改
  workflow spec hash 时补这条边。
- B-03(c)(d)：承接时改写 Plan 的 repositories path 且没有新的 G4；跳过目标 run 的 G0。后续仍有 G5/Review。
- B-22：8 个结构化字段的映射仍在代码中，未经真实 iPipe 返回验证；目前不生效，首次观测到时须先核对语义。
- A-13(a)：rerun 确认依赖 `startTime` 字段，需在真实网关上确认该字段存在且语义如预期。
- 403 被统一视为产物失效：若 token 权限错误也返回 403，会被误判为依赖产物不存在（`ENV_UNSATISFIED`）。

## 2026-10-25 · 评审第二轮：权威环境分类只认强证据；环境类禁止所有改代码路由（B-11、B-04 残留、A-11/12/13b）

- B-11：`clients/ipipe_runtime._classification` 不再把 stage/job 名称或消息里的 environment/capacity/runner/network/timeout/infra
  当作环境失败。只有结构化平台字段（job 的 `errorType`/`failType`/`failureType`/`systemErrorType`/`errorCategory` 取
  AGENT_OFFLINE、RUNNER_OFFLINE、RESOURCE_INSUFFICIENT 等 → `ENV_UNSATISFIED`，QUEUE_TIMEOUT/SCHEDULE_TIMEOUT → `ENV_TRANSIENT`；
  `agentStatus`/`agentState`/`runnerStatus` 为 OFFLINE/LOST/DISCONNECTED → `ENV_UNSATISFIED`）产生权威环境类（B-22 更正：这 8 个
  字段是候选映射，未经真实 iPipe 返回验证；当前观测到的响应均不含这些字段，因此实际不生效；首次在真实返回中观测到任一字段时须先
  核对语义再启用；代码映射保留）。关键词命中排在
  test/code 关键词之后，只记为参考类 `ENVIRONMENT_SUSPECTED`。`case test_syn_proxy_timeout assert failed` 现为 `TEST_FAILURE`，
  `network.c:12: error` 为 `ENVIRONMENT_SUSPECTED`，两者都能正常 REPAIR。
- 新增 `environment_evidence.py`：控制面自身证实的依赖获取失败才是权威证据——人工阶段参数解析时产物在所有精确 revision 候选上
  下载均 403/404（`ENV_UNSATISFIED`）或仅 5xx（`ENV_TRANSIENT`），由 `_stage_parameters` 记为
  `ipipe.dependency-failure:{run}:{build}:{module}`；`ipipe-rebuild` G8 材料中探测结论 `UNAVAILABLE` 的 build 同样成立。
- `PhaseProtocol._authoritative_failure_class` 只返回强证据：依赖失败记录 > IPIPE 证据中的 `repair_policy.RUNTIME_ENVIRONMENT_CLASSES`
  / `ENV_*` > 同规则的 FailureCase；参考类与历史遗留的 `ENVIRONMENT_FAILURE` 返回 None（交给诊断与 G6）。
- B-04 残留：权威环境类成立时，`repair_policy.ENVIRONMENT_FORBIDDEN_ROUTES`（`REPAIR`、`SPEC`、`GRILL`——route 枚举中会导向改业务代码的
  三项）在草案校验与完成阶段都返回 `ENVIRONMENT_REPAIR_FORBIDDEN`；允许 `STOPPED`、`DIAGNOSIS_INCOMPLETE`、`ARCHITECTURE_REVIEW`。
  诊断自报环境类时 schema 校验同样覆盖这三条路由。
- A-11：`IREPO-TOKEN` 值级规则接受分隔符后的空白与 `IREPO-TOKEN=`。
- A-12：`worker_driver.submit_draft` 对尚未记录草稿的 job 先 `redact_secrets` 再校验/哈希/存储，引用日志中的 Bearer 行不再导致草稿被拒；
  已记录草稿的 job（可能已有审批绑定其 hash）与 IMPLEMENT（内容是补丁本身）不改写，仍按原规则拒绝。
- A-13(b)：rerun 对账时，请求的 stage 若已被其他 intent 的回执认领，只有 startTime 严格晚于该回执时间才算本 intent 的执行。
- 测试：`test_code_only_repair_baseline` 新增 6 项（关键词误判可 REPAIR、依赖 404 拒绝 REPAIR/SPEC/GRILL、rebuild 探测 403 权威、
  结构化 runner 离线权威、route=SPEC 绕行被拒、STOPPED/ARCHITECTURE_REVIEW 可用），`test_secret_redaction` 新增 3 项，
  `test_ipipe_runtime` 新增 1 项；`test_relabelled_environment_failure_cannot_be_repaired` 的权威类改为 `ENV_UNSATISFIED`
  （`ENVIRONMENT_FAILURE` 不再是运行时权威类）。workflow spec hash 不变。
- 剩余风险：结构化环境字段（上述 8 个）是候选映射，未经真实 iPipe 返回验证；当前观测到的响应均不含这些字段，因此实际不生效；首次在
  真实返回中观测到任一字段时须先核对语义再启用。目前真实流量下权威环境类主要来自依赖失败证据与 `ENV_*`/控制面答复，其余环境失败
  走正常诊断 + G6；`DIAGNOSE -> ENVIRONMENT_BLOCKED` 边未加，环境类诊断止于 STOPPED；B-03 承接仍重写 Plan 仓库 path 且无新 G4、
  跳过目标 run 的 G0（其后仍有 G5/Review）。
- 补完（同日，接续中断的实现）：
  - 依赖失败证据单一来源：`ipipe-rebuild --request-g8` 探测 403/404 时由 `environment_evidence.record_probe_failure` 写入
    `ipipe.dependency-failure:{run}:{原 build}:{module}`；`proven_dependency_failure` 只读该前缀，不再把 G8 材料
    `ipipe.g8-material` 当证据（只读的 plan 步骤不写）。此前已存的 rebuild G8 材料不再单独构成权威环境证据，相应诊断回到 G6 判断。
  - 产物下载判定收紧：每个精确候选都须带状态码；全为 403/404 → `ENV_UNSATISFIED`，403/404/5xx 混合或全 5xx → `ENV_TRANSIENT`；
    任一候选无状态码（缺 irepo token、本地错误）或为其他状态码（401 等）→ 不记录。原实现只看去重后的状态码集合，一个 404 加一个
    缺 token 的候选也会记成 `ENV_UNSATISFIED`。
  - A-12：已记录草稿的 job 再次提交时，若脱敏后与已记录草稿完全相同（同一 hash），按脱敏内容消费，原样重放审批后的草稿不再被
    `PERSISTENCE_SECRET_REJECTED` 拒绝；内容不同的重提仍不改写。
  - `ipipe_watch` 通知的修复方向补充 `ENVIRONMENT_SUSPECTED`（参考类，照常诊断）与 `ENV_UNSATISFIED`/`ENV_TRANSIENT`。
  - 测试：`test_code_only_repair_baseline` 新增 8 项（`test_b11_*`、`test_b04_non_code_routes_stay_open_under_an_authoritative_class`），
    `test_a_rebuild_probe_403_is_authoritative` 改为“仅 G8 材料不算、记录后才算”；`test_ipipe_rebuild`、`test_stage_parameters`、
    `test_diagnosis_contract` 各新增 1 项接线测试；`test_secret_redaction` 新增 2 项。workflow spec hash 不变。

## 2026-10-25 · 评审跟进：rebuild 本地错误不再悬挂；job 级参数判断统一；产物候选逐个校验

- `ipipe-rebuild` 调用点本地错误：`state_store._local_error_observed` 现在同样接受 `operation=ipipe.trigger` 的 intent 与
  `ipipe.rebuild.write_error` 观察（同样要求 `before_dispatch is True`、无 `http_status`、无 `ipipe.rebuild.response`）。
  调用点错误后 trigger intent 以 `local_error_proven` 关闭，下一次申请取 attempt+1，不再永久 `QUERY_REQUIRED`。
  原来吞掉 `ValueError` 的静默处理改为返回 `REBUILD_LOCAL_ERROR_UNRECORDED`（带 `abandon_error`，intent 仍开放，需对账或
  `abandon-intent`）；成功关闭时 `REBUILD_LOCAL_ERROR` 带 `intent_closed: true`。
- 新增共享函数 `stage_parameters.job_override_decision`，`IpipeRuntime.stage_parameter_names`（G8 材料准备）与
  `_rerun_binding`（hash/写入）都用它：继承自其他 stage、嵌套 job 或 `upStageParam` 的参数没有可用写入，一律
  `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`（返回 `parameters` 与 `allowlisted_parameters`），不论是否在
  `known_job_override_names` 白名单内、平台能力声明为何；stage 自身声明但不在 manualParams 的参数，仍需白名单或已证实的
  override 契约。此前白名单让继承参数在材料阶段显示可执行、到 hash/执行才被拒。
- `product_policy.select_product`：精确匹配冻结 revision 的候选按顺序逐个做下载校验，前一个失败继续下一个；全部失败才返回
  `PRODUCT_DOWNLOAD_UNVERIFIED`，附 `candidates`（build/stage/job id、状态码、原因码）与 `http_statuses`，不含 URL。
- 测试：`test_ipipe_rebuild.LocalErrorTests`（3 项）；`test_product_policy` 新增 `test_allowlisted_inherited_parameter_is_refused_when_material_is_prepared`、
  `test_parameters_declared_on_the_stage_itself_stay_executable`、`test_a_failed_download_moves_on_to_the_next_exact_candidate`、
  `test_only_when_every_exact_candidate_fails_is_the_failure_returned`；原"白名单继承参数放行"与"能力声明即放行"两处断言按新合同改写，
  依赖继承形状的 proof 校验测试改用 stage 自身声明的夹具。workflow spec hash 不变。

## 2026-10-25 · 两份独立评审确认缺陷修复（A-01…A-06、B-01…B-08）

- A-03 `append_existing_cr` 永远拿不到回执：`IcodeRuntime._reconcile` 先做精确 revision 判断（目标 CR 当前 patchset 等于
  本次 `commit_revision` 且 CR 号一致即 `OK` 并写回执），仍指向其他 revision 才是 `CR_APPEND_ALLOWED`。push 后对账、
  重启重放都能确认；目标 CR 被他人改到别的 revision 时仍为 `SUBMIT_CONFIRMATION_REQUIRED` / `QUERY_REQUIRED`。
- B-01 CODE_ONLY 修复回 IMPLEMENT 的 baseline：`DIAGNOSE -> IMPLEMENT` 事件按 PLAN->IMPLEMENT 的形状 pin 该任务已被接受的
  Plan（`plan_artifact_id`/`plan_content_hash`），`source_revisions`/`baseline_revisions` 取 Plan 的仓库 revision，诊断的候选
  revision 另记为 `repair_source_revisions`。Plan 未被接受或不属当前 TASKS 时仍回 PLAN。新增共享函数
  `phase_protocol.plan_baseline_revisions` / `plan_implement_binding`，`adopt-code-only-repair` 同样使用。
- B-05 被取代 build 进入发布证据：新增 `build_markers.py`（`is_superseded` / `is_rejected` / `is_excluded`，唯一定义）。
  `pipeline_plan.successful_builds`（因而 `release_builds`、RELEASE 前驱、G9 绑定、worker 发布核验、进度快照）、
  `_ipipe_passed_modules` 与 IPIPE 证据摄取（`BUILD_SUPERSEDED`）都排除被取代/被拒 build；被取代 build 的 stage 不能再
  `ipipe-rerun`（`STAGE_BUILD_SUPERSEDED`）。IPIPE 态发起 `ipipe-rebuild` 时，被替换 build 必须有失败证据或刚被探测证实产物失效
  （`REPLACED_BUILD_NOT_FAILED`）。
- A-01 未知结果被误判为已启动：`_rerun_started` 要求 startTime 严格晚于 intent 创建时间减 30 秒容差
  （新常量 `_RERUN_START_CLOCK_SKEW_SECONDS`）；仅有 SUCCESS/RUNNING 状态不算证据；已被其他 rerun 回执占用的后继 stage 不采信。
- A-02 `--local-error-proven`：`write_error` 观察记录 `before_dispatch`（调用点 traceback 判断），`_local_error_observed` 要求其为
  true；无该字段的旧观察不能用于新的放弃。已写入的旧放弃回执（如 d63d8af4）只在其 retry 已存在时继续指向该 retry，不授权新的重发。
- A-04 `TRIGGER_MODE_MISMATCH` 后无出路：新 build 记 `ipipe.build-rejected:{run}:{build}`（独立于 superseded），discover/plan/watch/
  产物/发布全部排除；回执保留；返回 `build_rejected` 与 `next_step`（再次 `--request-g8` 生成 attempt+1 或改 `--event-type`）。
- A-05 同 stage 两个 G8 并发双发：`StateStore.claim_exclusive_intent`（同一 IMMEDIATE 事务内检查 `ipipe.rerun:{run}:{stage}`
  前缀下无无回执 intent 再插入），rerun 改用它；CLI `ipipe-rerun` 执行与 `--reconcile-only` 纳入 worker 同一 run 级租约
  （`_under_run_lease`，复用 `recovery.locks` 与 `worker-run:` 键）。
- A-06 凭据可能落库：`persistence_policy` 新增 `redact_secrets` / `redact_text` / `register_secret`，并在 `ensure_persistable`
  增加值级检查（`IREPO-TOKEN:`、`access_token=`、Authorization/Bearer、已加载 token 原值；与既有策略一致：发现即拒绝）。
  观察、回执、G8 与重建审批材料、iPipe 错误体在写入前统一脱敏；`load_tokens` 与 iPipe transport 注册 token 原值（仅内存）。
- B-02 修复轮次没有绝对上限：ARCHITECTURE_REVIEW 只重置 `THREE_FAILED_FIXES` 与 `NO_PROGRESS`（按 epoch 计）；`REPAIR_LIMIT`
  （≥5 轮，`repair_policy.REPAIR_ROUND_LIMIT`）按整个 run 累计，含承接继承的轮次。
- B-03 `adopt-code-only-repair` 前置校验：继承源 run 已消耗轮次（`inherited_repair_rounds`），预算耗尽拒绝
  `ADOPTION_BUDGET_EXHAUSTED`；只认该任务最新诊断（`ADOPTION_DIAGNOSIS_NOT_LATEST`）与当前 DAG 下已接受的 Plan
  （`ADOPTION_PLAN_NOT_CURRENT`）；已承接目标只允许同一对 (诊断, Plan) 幂等重放，否则 `ADOPTION_CONFLICT`；提交回执必须带匹配
  `task_id`；进入 IMPLEMENT 改为 CAS 的受控迁移（`policy_decision.controlled = CODE_ONLY_ADOPTION` 记录 G6/诊断/Plan 证据），
  不新增 workflow 边。
- B-04 环境失败守卫：以 iPipe 运行时记录的分类（IPIPE 证据，次选 FailureCase）为准，诊断改写分类也不能 `REPAIR`
  （草案阶段即 `ENVIRONMENT_REPAIR_FORBIDDEN`，完成时 STOPPED）；`PIPELINE_TRANSIENT` 加入 `NON_REPAIRABLE_FAILURES`；
  环境类集合统一为 `repair_policy.ENVIRONMENT_FAILURE_CLASSES`。`DIAGNOSE -> ENVIRONMENT_BLOCKED` 未加入（会改变 workflow hash），
  环境类诊断仍终止于 STOPPED，文档已写明。
- B-06/B-07/B-08 文档：phase-protocol.md 改为"发布 revision 必须等于冻结 revision，否则 `PRODUCT_REVISION_MISMATCH`"；
  diagnosis.schema `repair_scope` 描述、state-machine.md、SKILL.md、failure-taxonomy.md 与上述行为对齐；README 补
  `adopt-code-only-repair`、`ipipe-rebuild` 新约束、`abandon-intent --local-error-proven`、并发 rerun 与脱敏说明。
- workflow spec hash 不变（`76bb964c…d410`）：未改 `workflow_spec.py`；schema 描述不参与 hash。
- 测试：新增 `test_code_only_repair_baseline.py`、`test_superseded_build_evidence.py`、`test_secret_redaction.py`、
  `test_adopt_code_only_repair.py`，并在 `test_icode_runtime`、`test_ipipe_runtime`、`test_ipipe_rebuild`、
  `test_state_and_artifacts`、`test_phase_protocol_repair` 中补回归。`FakeApi` 为缺 `startTime` 的执行中阶段按首次读取打时间戳
  （状态不变则时间不变），以符合平台语义。

## 2026-10-25 · 补记：此前已落地但未入账的变化（实际落地日期未记录，本日核对后补齐）

- CODE_ONLY 修复直接回 IMPLEMENT：`repair_scope == CODE_ONLY` 且任务有可复用 Plan 时 `DIAGNOSE -> IMPLEMENT`，新 Change Set
  仍需 G5/Review；无 Plan 的旧 run 回 PLAN。（本日 B-01 修正其 baseline 与 Plan pin。）
- ARCHITECTURE_REVIEW 作为修复预算边界：最初实现为整体 epoch 清零；本日 B-02 改为只重置"三次失败升级/同签名无进展"，
  5 轮上限按 run 累计。
- 环境失败禁止 REPAIR 守卫：`ENVIRONMENT_FAILURE`、`ENV_UNSATISFIED`、`ENV_TRANSIENT`、`JOB_PARAMETER_OVERRIDE_UNSUPPORTED`、
  `COMPILE_ONLY_TRIGGER_UNSUPPORTED` 的诊断 route=REPAIR 被 schema 与完成路由拒绝（`ENVIRONMENT_REPAIR_FORBIDDEN`）。
  （本日 B-04 改为以运行时分类为准并加入 `PIPELINE_TRANSIENT`。）
- `adopt-code-only-repair`：把已停止 run 中获 G6 的 CODE_ONLY 修复承接到新 run，只复制 TASKS/PLAN，不复用审批。
  （本日 B-03 补齐前置校验。）
- `tom-autodev/references/changelog-policy.md`：IMPLEMENT 必须为每个改动源码的仓库在同一 change-set 内追加 changelog 条目，
  Review 将缺失列为阻断 Standards finding；tom-implement / tom-review 的 SKILL.md 引用该策略。
- `prepare-change-set` 在 baseline 之上超过一个提交时返回 `MULTIPLE_COMMITS_ABOVE_BASELINE`（iCode HCK_F507），
  在 G5/G7 之前失败而非等到 SUBMIT。
- `_architecture_reentry_task`：ARCHITECTURE_REVIEW 经 rewind 进入 TASKS（不带 task_id）后，WORKSPACE/PLAN 沿
  `source_event_id` 审计链找回被重开的任务；语义未变的 DAG 不再让旧 Review 把该任务藏出前沿。

## 2026-10-25 · 修复显式命令消费审批后遗留 APPROVAL_RESUME handoff（`MULTIPLE_RESUME_HANDOFFS`）

- 根因：`approve` 在批准时按 run 当时的最新事件创建 `APPROVAL_RESUME` handoff；`migrate-workflow-spec --approval-id`、
  `ipipe-rebuild --approval-id` 等显式命令直接消费审批，却不完成该 handoff，且 `ipipe-rebuild` 随后追加 IPIPE 事件，
  使 handoff 指向旧事件、永远不会被 `resume` 消费。多条 PENDING 并存时 `continue` 一律返回 `MULTIPLE_RESUME_HANDOFFS`。
- 源头：新增 `StateStore.complete_approval_handoffs` / `complete_handoff_audited`（复用 `complete_handoff`，幂等，只完成
  `payload.approval_id` 等于该审批的 handoff，并写 `handoff-completion:{id}` 审计：actor、reason）。命令成功后调用：
  `migrate-workflow-spec` 应用、`ipipe-rebuild` 触发/对账换绑、`ipipe-rerun`（含 `--reconcile-only`）、`submit`、
  `advance`、`repin-profile --approval-id`、`rewind --approval-id`、`optimize apply`。失败结果不完成 handoff。
- 兜底：`worker_driver.resume`、`continue` 与 Stop-hook `auto_resume_from_hook` 在候选 >1 时先完成"已过期"的 handoff
  （reason=`STALE_EVENT`）：`payload.event_id` 不等于最新事件，且审批仍是绑定同一 input_hash 的 APPROVE，并且已被后续事件消费
  或其 gate 与当前状态不符。匹配最新事件的 handoff、REJECT/TIMEOUT 审批的 handoff 一律保留；清理后恰好 1 条才继续，
  0 条为 `NO_RESUME_HANDOFF`（`continue` 此时转入 `advance`），仍 >1 为 `MULTIPLE_RESUME_HANDOFFS`。
- 新增 `tests/test_stale_resume_handoffs.py`（13 项）。

## 2026-10-25 · `ipipe-rebuild`：编译产物失效后对同一 CR 版本受控重新构建

- 场景：失败 build 的编译产物已不可下载（HTTP 403/404），该 build 的任何 stage 重跑都会在第一个环境任务失败，
  `ipipe-rerun` 只是在消耗 G8。新增 `ipipe-rebuild RUN --reason ... [--module M] [--event-type GERRIT_CHANGE|NEW_COMMIT]`：
  默认只读预检；`--request-g8` 保存准确材料并投递 G8、不执行；`--approval-id A --input-hash H` 触发一次并换绑；
  加 `--reconcile-only` 只用 GET 认领已发出的触发。
- 前置条件（fail closed，`REBUILD_NOT_JUSTIFIED`）：run 在"IPIPE 失败后的 DIAGNOSE"或 IPIPE；诊断的正是当前被跟踪的 build；
  只读探测其 `AGILE_COMPILE` 产物，确认平台答复 403/404（200 为仍可下载，5xx/超时/缺 token 为无法判定，均拒绝）；
  业务 CR 当前 patchset 必须等于冻结 revision（`REVISION_DRIFT` / `REVISION_UNVERIFIED`）；无未决触发 intent（`QUERY_REQUIRED`）。
  探测不输出产物 URL、签名地址或 token，只给状态码。
- 审批：沿用 G8，hash 绑定 run、pipeline、模块、`revision_set_id`、仓库清单、环境指纹、原构建号、attempt 序号、
  触发类型与原因；材料另含探测结论与 CR 核对结果（不入 hash）。执行时在写之前再次复核产物状态与 CR。
- 触发：复用 `trigger_by_revision`（新增可选 `event_type`，即 `ipipe-cli trigger run --event-type`，默认不改变既有请求）。
  幂等键 `ipipe.trigger:{run}:{pipeline}:{revision_set_id}:attempt-N`；历史无后缀键即 attempt-1，原样保留，仅被适配的 build 同样记作 attempt-1。
  响应无法解析时记录脱敏观察并按 revision + 时间窗口只读对账：只认 id 大于原 build、revision 集合匹配、本 run 尚未拥有的 build，
  多个候选返回 `BUILD_AMBIGUOUS`，没有候选保持 `QUERY_REQUIRED`，不重发。若平台上已有这样的更新 build（如从 iPipe 页面手动发起），
  直接认领而不再触发第三个。
- 换绑（单事务）：追加一条 IPIPE 审计事件（克隆入口 payload，冻结计划与 G7 绑定不变，附 `rebuild` 审计记录）、写
  `ipipe.build-superseded:{run}:{旧 build}` 标记、把挂起的 DIAGNOSE producer job 置为 `SUPERSEDED`（保留行与草稿，不可再 claim/fulfill）；
  之后记录新 build 的全部 stage 归属。旧 build 的事件、回执、artifact、stage 绑定一律不改。`discover`、产物选择、`watch-ipipe`
  都忽略被取代的 build。新 build 的 `trigger.ciType` 与原 build 不同返回 `TRIGGER_MODE_MISMATCH`，回执保留但不换绑。
- 状态机：新增受控边 `DIAGNOSE -> IPIPE`（证据 `ipipe-rebuild` + G8），G8 标签补充"重新触发整条流水线"。**workflow_spec_hash 因此改变**，
  已 pin 的在途 run 会报 `WORKFLOW_SPEC_DRIFT`，需先 `migrate-workflow-spec RUN --request`、批准、`--approval-id` 应用。
- 新增 `tests/test_ipipe_rebuild.py`（38 项 fake 测试）；`StateStore` 新增 `idempotency_results_with_prefix`、
  `commit_transition_result(..., supersede_jobs=)`，`fulfill_producer_job` 拒绝 `SUPERSEDED` job。
- 未验证：`eventType` 在 `trigger_by_revision` 请求体中的位置按 ipipe-cli 推断，从未对真实网关执行过写入；
  测试 CR 无 open 状态时其 patchset 只能报告"未核对"，不阻断。

## 2026-10-23 · 未变更模块发布产物必须与冻结 revision 一致；本地编程错误放弃的 rerun 可重发一次

- 根因：`product_policy.select_product` 对未变更模块的 `AGILE_RELEASE` 只比较构建与 job 自身的 revision，
  从不与 `pipeline_plan` 冻结 revision 比较，于是取分支上最新成功发布；证明里的 revision 变成新提交，
  而 binding 的仓库清单仍写冻结值，两者无人校验。现在在最近 30 条成功 BranchPipeline 构建中查找 revision
  精确等于冻结值的发布，找不到返回 `PRODUCT_REVISION_MISMATCH`（带已见 revision 前 8 位），不回退到最新，
  并走原有"缺发布产物 → compile-only 受阻"路径。`_rerun_binding` 另行核对 proof revision 与冻结值、
  binding 仓库 revision 一致，不一致返回 `PRODUCT_PARAMETER_BINDING_MISMATCH`。该变化会使依赖旧结果的
  G8 `input_hash` 变化，需重新申请审批。
- `abandon-intent --local-error-proven`：仅当 `ipipe.rerun` 的 intent 已有 `ipipe.rerun.write_error`
  观察（AttributeError/TypeError/NameError、无 `http_status`、无响应观察）时，才在放弃回执写入
  `local_error_proven=true`。带该标记的放弃让同一审批得到 `:retry-1` 键重发一次（每个审批最多一次，
  重发前仍复核观察）；无标记的放弃仍是终局。运行时自己关闭的本地错误 intent 也带该标记。

## 2026-10-22 · iPipe 重跑：本 stage 自有参数走 manual_execute_stage，本地错误不再留"未知" intent

- 根因：`dependency_declaration` 把"不在 manualParams 的必需名"一律标为 `requires_job_override`，
  包括声明在本 stage 自己 paramBeans/inParamsMap 的名；`rerun` 随后调用套件内不存在的
  `execute_stage_job_override`，在 claim 之后本地抛 `AttributeError`，被当成 `RERUN_RESULT_UNKNOWN`，
  POST 从未发出。已删除该悬空调用，一律使用 `manual_execute_stage`。
- 新增 `stage_parameters.job_scope_names`：仅当名的声明 scope 为 `stage:{stageConfId}` 且
  `upStageParam` 非真时视为本 stage 自有；上游/嵌套 job/`upStageParam` 仍在 `_rerun_binding`
  即返回 `JOB_PARAMETER_OVERRIDE_UNSUPPORTED`（G8 与 claim 之前）。`parameter_declaration`
  内容未变，既有 G8 的 input_hash 不受影响。
- `rerun` 在 claim 前解析 `manual_execute_stage` 并校验可调用与参数形状，失败返回
  `RERUN_LOCAL_ERROR`（`retry_allowed=false`，不创建 intent，同一 approval 仍可用）。claim 后若错误由
  调用点本身抛出（traceback 无被调方栈帧），写 `INTENT_ABANDONED` 回执并返回 `RERUN_LOCAL_ERROR`；
  真正进入客户端后的超时/未知仍为 `RERUN_RESULT_UNKNOWN`。
- 新增测试：本 stage 声明走一次 POST、上游/`upStageParam` 无 POST 无 intent、本地错误不留未决
  intent、runtime 引用的 `api.*` 方法均存在于真实 `IpipeApiClient`（`reconcile_branch_compile`、
  `trigger_branch_compile` 受 `compile_only_capability` 保护，真实适配器恒为不支持）。

## 2026-10-14 · iPipe 重跑未知结果留证据，reconcile 区分"未启动但未证明被拒"

- 只读核查 ipipe-cli 0.2.0：二进制中阶段写入路径仅 `/api/agile/v1/stageBuilds/build`
  （同时含 `AGILE-PLAT-NAME/TOKEN`），另有 `job-builds/cancel/%d`；无独立 retry 路径，
  `build retry` 帮助为"重新执行/手动执行阶段"。证据为字符串级，未经反汇编绑定到函数，故不新增
  端点、不按 FAIL/PENDING 切换端点，现有 `manual_execute_stage` 保持不变。
- 重跑写入出错（含超时/5xx/非 JSON 2xx）时向 intent 追加 `ipipe.rerun.write_error` 观察：
  HTTP 状态、错误类型、顶层字段名、业务 code、<=200 字符且已去 URL/token 的片段；不含 token、
  签名 URL、完整响应。传输层为 `IPIPE_RESPONSE_INVALID` 补充 `response_type/body_chars`。
- `reconcile_rerun` 新增 `RERUN_NOT_STARTED_UNPROVEN`：超过 300s 宽限且阶段状态、起始时间未变、
  无后继 run 时返回，`abandon_eligible=true`、`rejection_proven=false`、`retry_allowed=false`，
  并追加 `ipipe.rerun.reconcile` 观察。宽限内仍为 `QUERY_REQUIRED`；已启动仍为 `OK` 回执。
- 已知约束：同一绑定对应同一 approval_id，放弃 intent 后旧 G8 只会重放 `INTENT_ABANDONED`，
  重新发起必须是不同的已批准绑定（如参数变化）。

## 2026-10-14 · 同一 iPipe 阶段可在新 G8 下再次重跑

- `ipipe.rerun` 幂等键加入 approval_id（`ipipe.rerun:{run}:{stage}:{approval_id}`），
  修复同一 stage 第二次合法重跑被 RERUN_CONFLICT 拦截；同一授权重放仍不重复 POST。
- 旧格式键（无 approval 维度）仅对创建它的审批继续有效，对其他审批视为"先前尝试"。
- 新增前置：该 stage 先前存在已 claim 但无 receipt 的 intent 时拒绝新重跑（QUERY_REQUIRED），
  须先 `--reconcile-only`（只 GET，按传入 approval 精确定位 intent）。

## 2026-10-12 · 人工 iPipe 阶段自动发现参数并准备 G8

- 新增冻结 changeset/plan 的产物规则，涵盖非发布门禁测试模块。未变模块仅取目标 branch
  BranchPipeline 成功 release job 的实际 revision/发布 URL；有变模块仅取本 run owned、
  exact candidate ChangePipeline compile。校验阶段/job 身份和 gzip 下载，不接受历史 compile/latest。
- 只读实测 bgwagent BranchPipeline 发布产物可查询且可下载；下载为 307 签名重定向，
  第二跳不转发 IREPO-TOKEN，不持久化签名 URL。补齐已文档化 listing name/token 必需参数。
- P0 空 manualParams 仍发现 job shell 和上游 paramBeans 依赖，定位 get_bgwagent 静态默认
  node97110098；原 G8 params {} 未动态覆盖。真实 API 的准确 job override 与 compile-only
  尚无契约证明，显式 blocking；新触发协议仅由 fake 验证，不编造平台字段或修改模板。
- compile-only 的 reviewable 实现绑定 run/plan/module/branch/revision/scope/capability，先持久化
  intent，未知结果仅查回；新增 fake 回归。当前业务 run、profile pin、审批及外部配置未改变。

- 修复执行应答仅解析顶层 ID 的缺口：支持嵌套身份，缺身份时 GET 确认，不重复 POST；
  保存不含原始内容的应答形状观察。新增 `--reconcile-only`，准确 G8 下仅查询既有意图并补确认。
  阶段快速失败可按晚于 claim 的 startTime 确认执行；旧失败/等待状态不作为确认。

- `continue` 对 MANUAL_WAIT 查询真实 stage 参数声明，按项目/profile 的准确映射解析产品，
  准备 exact hash 的 G8 并使用既有审批投递入口；批准后执行并恢复监控。
- `ipipe-rerun` 省略 `--parameter` 时查询声明，空参数须平台明确证明；新增 `--request-g8`，
  将脱敏 binding、声明、映射及产品 revision 保存为审批材料，不批准或执行阶段。
- 移除参数 basename/子串推断，未知映射和缺失产品/token 列出缺失项并阻断。
  BGW 普通 P0 与新 case 的 skill 文档分别说明，profile schema 支持准确 stage 映射。
- 定向验证只使用 fake 控制面；当前真实 stage 的读取不作为业务回归通过证据。

## 2026-10-09 · watcher 不再为已结束的 run 刷日志

- `work_card.refresh_all` 每个 watcher tick 遍历全部 run；已结束（STOPPED/RELEASE_SUCCESS）的 run
  每次仍输出 `WORK_CARD_UNCHANGED/UNAVAILABLE`，且 spec 漂移的旧 run 每次输出 `WORKFLOW_SPEC_DRIFT`，
  导致 `watcher.log` 无休止增长。现对已结束 run 静默这三类无变化结果；真正的卡片更新仍会上报。
  进行中的 run 行为不变。
- 新增回归测试 `test_work_card.py::test_refresh_all_is_silent_for_finished_runs`。
- 运维：`watch-approvals` 需常驻（`bin/supervise.sh`），否则 如流批准只能在下次 `continue` 时才落账。

## 2026-10-08 · G4 校验补齐：PLAN 仓库路径必须是绝对检出路径

- 关闭一个 G4/IMPLEMENT 校验缺口：PLAN 阶段此前只校验 `repositories[].role` 与 `revision`，
  不校验 `path`，导致把 `path` 写成模块名（如 `baidu/sysip/x86bgw`）的计划能通过 G4 审批，
  直到 IMPLEMENT 的 `prepare_change_set` 才以 `PLAN_REPOSITORY_MISMATCH` 失败。
- `phase_protocol.py` 的 PLAN 分支新增校验：每个 `repositories[].path` 必须是非空的绝对路径，
  否则返回 `PLAN_REPOSITORY_MISMATCH`（`content_invalid`）。profile 检出路径即绝对路径，
  IMPLEMENT 仍是“路径↔profile↔worktree”的权威绑定；本校验把该约束提前到门上，使 G4 与 IMPLEMENT 一致。
- 新增回归测试 `test_producer_validation.py::test_plan_rejects_module_name_repository_path_at_g4`。

## 2026-10-08 · 停止run 改名、Comate 直接提意见、如流即时回执、G6 卡精简

- 「驳回」更名为「停止run」：按钮、回复指引、回执文案统一为“停止run/停止”，语义仍是停止本 run；
  如流回复解析新增 `停止run`/`停止` 别名，`驳回`/`REJECT` 继续兼容。
- Comate 直接提意见通路：`/tom-autodev CARD 需修改 <意见>` → `revise --feedback` 可对仍为
  PENDING 的门以 comate 渠道登记“需修改”并原地重做，无需先在如流点按钮；门已同意/超时则拒绝。
- 如流即时可视化回执：网关在收到“需修改”后同群审批人发来的普通意见时，立即把该审批卡刷新为
  “已收到修改意见…”，不改变决策（决策仍由 watcher 按绑定登记）。
- 非可修改门不再显示“需修改”按钮：迁移、变更请求、profile 重钉、提交/流水线/发布等门只有
  “同意/停止run”。台账层也拒绝对这些门的 REVISE（`APPROVAL_REVISE_NOT_SUPPORTED`，门保持待审批），
  避免误点“需修改”把门卡死；网关据 `revisable` 标志绘制按钮，旧卡缺该字段时默认仍带“需修改”。
- 修复误点“需修改”再“同意”把门卡死的隐患：如流回复消费改为门感知，对非可修改门的“需修改”
  回复直接跳过，不再遮挡其后的“同意”，也不会把被台账拒绝的“需修改”缓存成结论
  （action 透传给回复消费）。新增消费与网关按钮测试。
- 清理残留的“驳回”展示：卡片“按钮含义”“决定边界”、run 简报的“已驳回”均改为“停止run/已标记需修改”，
  修正把 REVISE 误写成“驳回后按意见重做”的本次修改说明；`停止`/`停止run` 加入决策命令识别，
  收集意见时遇到“停止”也正确终止。仅保留 `驳回`/`REJECT` 作为输入别名与历史兼容说明。
- 审批卡精简（所有门，非仅 G6）：“请先查看”区只保留“完整内容见审批正文＋材料包”指针，不再罗列
  input_artifacts / source_evidence_refs / 各类哈希等与正文重复的原始标识；内容摘要与正文重复时
  不再重复；正文有内容时不再同时出现“未提供材料位置”的矛盾兜底。
- 修复 G6“材料待翻译”根因（诊断卡专有渲染）：翻译守卫原来把含标识符的中文整段判为未翻译，现以
  “连续 4 个及以上英文单词才算需翻译的句子”为界；失败签名按标识符原样展示；诊断支撑证据
  （日志/复现/最小验证）改为计数并指向材料包。
- G6（DIAGNOSE）纳入可原地重做的门：诊断是模型草案，对它“需修改”是对草案提意见而非停止 run。
- 控制面全量回归通过；tom-review、autodebug 回归通过；未执行 BGW 本地编译或测试。

## 2026-10-08 · 「需修改」按钮后直接回复意见

- 修复：watcher 处理“需修改”时调用 `revise` 缺少必填 `actor`，`TypeError` 被吞掉，点按钮
  后只记账不重做；且它塞入固定占位意见，从未向审批人要真实意见。
- 点按钮（或只回复 `需修改 <id>`）后，同一审批人在同一群的后续普通消息即为修改意见，
  直到其下一条审批指令；其他人、其他群的消息不计入。缺意见时回执提示“直接在本群回复
  修改意见”，watcher 每轮检查，收到后登记重做并回执“已收到修改意见”。
- 卡片点击状态与审批正文同步说明该用法；网关改动需重启网关进程生效。
- 修复：期间对其他审批的同意/驳回不再截断意见（BGW-1995 先批迁移再发意见即被丢弃）；
  只有对本审批的再次指令或对另一审批的“需修改”结束收集。
- 控制面全量回归 1235 项通过；tom-review、autodebug 回归通过。

## 2026-10-08 · 需求变更回到 GRILL，G5 需修改原地重做

- 根因（BGW-1995）：G1 之后用户取消 c2s_mss 严格等值，但诊断只能路由 PLAN/SPEC，回退
  在进入代码阶段后被禁止，决策日志 B2 从未修改；PLAN/SPEC 以决策日志为准，每次重出都恢复
  严格等值，诊断批准的修复方向也没有交给下游 producer。
- diagnosis `route` 新增 `GRILL`，新增 `DIAGNOSE → GRILL`（需 G6）。
  `USER_REQUIREMENT_CHANGED` 只能路由 `GRILL`/`STOPPED`，`GRILL` 只用于该分类；入口阻塞报告
  已声明需求变更时，诊断改分类以 `REQUIREMENT_CHANGE_RECLASSIFIED` 拒绝（草案可修正）。
- 诊断完成进入 GRILL/SPEC/PLAN 时，事件携带 `reentry`（`kind: DIAGNOSIS`、修复方向、修复
  计划、诊断 artifact），producer 的 `revision_context` 与审批卡“本次修改说明”都会展示。
- G5 加入可原地重做的门：`需修改` 后重新进入 IMPLEMENT，钉住被驳回 Change Set 的 PLAN
  与 baseline revision；缺 PLAN 绑定时以 `PLAN_PIN_REQUIRED` 拒绝。
- producer 合同：汇报草案只能依据已保存内容；用户后续要求与已批准决策日志/Spec 冲突时
  必须上报 `USER_REQUIREMENT_CHANGED`，不得静默采纳或静默沿用上游。
- workflow policy 变化：进行中的 run 需经 `migrate-workflow-spec` 审批后续跑。
- 控制面全量回归 1232 项通过（新增 `test_requirement_change_route.py`）；tom-review、
  autodebug 回归通过；未执行 BGW 本地编译或测试。

## 2026-10-08 · Change Set 受控生成、PLAN 冲突上报与已批准迁移续跑

- 新增 `prepare-change-set`：校验当前 IMPLEMENT action/job、Plan 前驱、baseline、worktree
  所有权与干净 HEAD 后，只读提取确定性的完整 Git diff 并计算哈希；`--output` 独占创建。
- change-set schema 新增 `patch_kinds`（`git-diff-v1` / `no-op`），校验器用
  `git apply --numstat` 解析 patch，拒绝 SHA、路径、摘要和占位文本；IMPLEMENT 草案额外
  校验 traceability 与 Plan 验收点、测试 ID 一致。
- 新增 `report-producer-conflict` 与 `producer-conflict` schema：PLAN 与前序契约冲突时以
  `SPEC_CONFLICT` 转入 DIAGNOSE（`PLAN → DIAGNOSE` 需 producer-conflict 与 task-dag）。
- `continue` 可消费精确匹配当前 run 与迁移计划 `input_hash` 的 `WORKFLOW_SPEC_MIGRATION`
  批准并应用迁移；`status` 显示「迁移已批准待应用」。不需要人工门的已保存草案显示「待处理」。
- KU：索引发布意图记录编辑意图链字段，新增外部只读观测表 `external_observations`；
  KU 意图未决时 `drive` 自动持有 worker lease；旧版无链字段的 publish 意图仅在同 run
  存在更早的精确 marker 编辑时才恢复。
- 修复：`_publish_matches_edit` 误声明为 `staticmethod` 却引用 `self`，旧版 publish 意图
  恢复分支必现 `NameError`；`report-producer-conflict` 未校验报告 `job_id` 与当前 job 一致。
- `tom-implement` 要求通过 `prepare-change-set` 生成候选，不手工拼 patch 或哈希。
- 控制面全量回归 1224 项通过；未执行 BGW 本地编译或测试。

## 2026-10-06 · 审批按钮补发、进度失败标记与工作流审查修复

- 恢复 11:11 意外丢失的诊断审批中文化与 `correct_approval_message`（工作区文件被旧
  版本覆盖，`orchestrator.py` 仍引用该函数，导致 `correct-approval-message` 导入失败）。
- 审批按钮：纠正消息和截止前提醒只发 Markdown，按钮停在上方原卡片。现在两者都会在
  下方补一张同一审批的按钮卡（网关 `/card/approval` 支持可选 `presentation`，生成独立
  卡片 id）；审批人在任一张卡点击后关闭同审批的其它卡；同一卡的刷新串行执行，修复连点
  两次时平台返回「刷新外层气泡卡失败」，并防止已处理卡片被重复点击重新画回按钮。
- 进度路线：`progress_snapshot` 把事件链上所有到达过的阶段都标为完成，IMPLEMENT 被
  producer-block 阻断进入 DIAGNOSE 时仍显示「代码实现✓」，回退 PLAN 后下游阶段也仍为 ✓。
  改为按事件链正向重放：进入 DIAGNOSE/恢复分支的前驱标 ✗，回退会重置其后阶段。
- `continue` 卡死：`_ensure_approval_resume_handoff` 会为当前闸门的任意历史 APPROVE
  （更早任务或更早 PLAN 轮次）补建 resume handoff，多张无人完成的 handoff 使后续
  `continue` 永久返回 `MULTIPLE_RESUME_HANDOFFS`。现在非本次落账的审批必须晚于当前状态事件。
- G6 诊断展示：未知英文的词级替换会删除 not/no/must/only，`Do not remove …` 被显示成
  「移除」。改为原样展示并标注「（未翻译原文）」；`tom-diagnose` 要求展示字段用中文书写。
- producer-block：阻断报告之后提交的真实 Change Set 会被下一次 `drive` 以阻断报告
  覆盖并转入 DIAGNOSE。现在仅在 job 仍为 PENDING 且无草案时消费阻断；同 job 有待消费
  阻断时拒绝提交 Change Set（`PRODUCER_BLOCK_PENDING`）；`report-producer-block` 持有
  worker lease；schema 要求 `workspace_state`。
- workflow drift：旧 DIAGNOSE 兼容恢复不再接受 `WORKFLOW_SPEC_DRIFT`，避免漂移状态下先
  认领 job、写模型回执后才被守卫拒绝；移除只用于绕过漂移守卫的 `allow_legacy_diagnose`。
- 纠正消息：提醒优先重放最近一次已送达的纠正正文；网关发送前拒绝时撤回投递意图。
- 运维：审批 watcher 原作为 agent 会话后台任务运行，10-05 23:10 随会话被终止，导致
  BGW-1995 G6#retry-1 的按钮点击只写入回复日志。已改为 `nohup` + 自动重启循环运行
  gateway 与 watcher，并补消费该点击（`ae62502d647f4286bde9b3b8f5f28d5d` 已 APPROVE）。
- 文档更正：历史 DIAGNOSE job 兼容恢复也接受已 FULFILLED 且带诊断草案的 job。
- 控制面全量回归 1207 项通过；未执行 BGW 本地编译或测试。

## 2026-10-05 · 漂移迁移审批消费与诊断前驱兼容恢复

- 明确 G5 路由边界：没有真实 IMPLEMENT Change Set 时，`PLAN_INCOMPLETE` 只能进入
  DIAGNOSE/G6，不创建空的 G5；诊断批准后回到 PLAN/G4，真实改动集生成后才开启 G5。
- 修复 G6 诊断审批的英文展示泄漏：摘要、诊断证据列表、根因、最小验证、修复计划、
  操作提示和异常长文本统一使用中文展示；英文枚举和 canonical JSON 仅保留在审计材料。
- 新增同一审批绑定下的受控消息纠正，复用原 `approval_id`、`input_hash`、审批期限和
  delivery intent，不创建重复审批；未超时审批仍禁止普通 reissue，纠正操作具备幂等和
  结果未知保护。
- 修复 workflow drift 下迁移审批无法落账的死锁：`receive_infoflow_reply()` 不再让
  方法级通用 drift 守卫先于审批 action 判断；仅对已核验的
  `WORKFLOW_SPEC_MIGRATION` 放行消费，普通业务审批仍 fail-closed。
- 迁移回复继续校验原 `approval_id`、run、input hash、审批成员、群组、时间窗口和送达
  回执；重复消费保持幂等，旧 watcher 的 `WORKFLOW_SPEC_DRIFT` 缓存结果不会永久污染
  同一回复 key。
- 修复 `IMPLEMENT → DIAGNOSE` 对嵌套
  `payload.evidence.producer_block_report_id` 的前驱查找；增加 run、task、source
  revisions、报告 artifact 和 hash 的一致性校验，避免伪造或跨任务复用 producer-block。
- 增加修复前已创建的合法 DIAGNOSE ProducerJob 的受限兼容恢复。仅当历史 job 仍为
  pending（或已 FULFILLED 且带诊断草案），且 run、事件、task、父证据和 revisions 完全一致时复用；其它 action identity
  冲突仍拒绝，不改写历史事件或数据库。
- 修复兼容恢复后的诊断提交丢失证据问题：只把当前已核验 producer-block artifact
  映射为精确 `artifact:<id>` evidence reference；历史空 refs、伪造 refs、错误 parent
  或 revisions 均继续拒绝，避免以兼容逻辑绕过 `EVIDENCE_REQUIRED`。
- 控制面定向回归：迁移/producer-block/执行守卫/审批通道 39 项通过，诊断前驱 10 项
  通过，历史 DIAGNOSE job 兼容恢复 13 项通过，证据兼容提交及诊断合同合计 36 项通过，
  `git diff --check` 通过。以上结果不代表 BGW 本地编译、单测或回归已执行；BGW 执行
  证据仍必须来自 iPipe。
- BGW-1995 已使用原 run 和真实诊断草案创建 G6 审批并停驻，未自动审批：
  `approval_id=979cb6b5a13343c09482035081b9ba20`，
  `approval_input_hash=6db6d754dbd92aca51a9af106e4809e686b166b94c03d0776eff277202c72918`。

## 2026-10-04 · G4 需修改自动续跑

- G4/PLAN 纳入可按意见重做的 Producer 阶段；选择“需修改”后输入“继续”会自动消费
  中文修改意见，重新生成新 hash 的计划并投递新的中文审批卡。
- 保留上一版计划、审批和修改意见的审计链；旧英文 `REJECT` 回复继续兼容。
- 更新状态提示、CLI 帮助、操作文档和 skill 规则，避免把 G4 误报为不可恢复。

## 2026-10-03 · KU 写入收敛与审批卡动作语义修复

- 修复 KU 根索引追加：普通说明段落不再误用 `selectionByTitle`，改用完整块内容锚点；
  已落库但索引回写失败的文档可通过受控 paragraph fallback 收敛，保留原始 intent、
  幂等键和外部收据。补充索引适配器回归。
- 修复 G4 审批材料重建使用 Intake 历史 `profile_hash` 的问题，改用当前 pinned profile
  hash，避免工作区、仓库基线和 ownership 均正确时仍报 `APPROVAL_MATERIAL_MISSING`。
- 如流审批卡统一提供“同意 / 需修改 / 驳回”三按钮，分别映射到 `APPROVE / REVISE /
  REJECT`。`REVISE` 保留 run 并按意见重做；`REJECT` 停止 run，不再把驳回命令当作修改入口。
- 超时提醒和重发沿用同一 `input_hash`，但创建新的 approval ID，并通过真实 Infoflow
  client 重新投递完整按钮；网关 `/health` 增加可核验的版本字段，当前网关版本为 `0.1.1`。
- 本轮专项验证：审批投递、watcher、网关、run-change 共 92 项通过；G4 材料回归 6 项
  通过；网关 Node 语法检查和 `git diff --check` 通过。未据此宣称真实如流点击资格。

## 2026-10-01 · 不重开 run 的返工：驳回重做与变更请求回退

- 新增 `revise`：G1/G2/G3 驳回后以该驳回为授权、带修改意见与上一版草案重新进入同一阶段，
  生成新 action/job/门；`context` 暴露 `revision_context`，新审批消息附修改说明与差异。
  如流 `REJECT <id> 意见` 中的意见只取实际投出驳回的成员。此前驳回是死胡同、意见被丢弃。
- 新增 `rewind`（`CHANGE_REQUEST` 门）：按 `REWIND_TARGETS` 回到 GRILL/SPEC/TASKS，
  可同时移动 `test_repo`/`business_repos`（仅未绑定过工作区的 run），绑定发起时事件，
  以比较后追加写入事件。ARCHITECTURE_REVIEW 不再是无出口的 `INVALID_STATE`。
- 被 revise/rewind 取代的旧门不再催办、超时重发（`APPROVAL_SUPERSEDED`）或计入待审批；
  驳回后 `status` 显示“已驳回待修改”并给出 revise/rewind/stop 指引，而不是让人 `continue`；
  `APPROVAL_RETRY_EXHAUSTED` 附出路说明。
- `REVISABLE_STATES` / `REWIND_TARGETS` 计入 `workflow_spec_hash`：已有暂停 run 需一次
  `migrate-workflow-spec` 迁移审批。

## 2026-09-30 · 漂移迁移闭环、点击身份与状态可操作性

- 修复 `WORKFLOW_SPEC_DRIFT` 下迁移门自身无法结算的死锁：投递、读回、落账、重发和
  watcher 结算统一按 `migration_exempt()` 豁免（含 `#retry-N`），其它门在漂移下仍一律
  拒绝。此前卡片能发出但决定永远不落账，`migrate-workflow-spec --approval-id` 恒返回
  `APPROVAL_REQUIRED`，暂停的 run 只能手改账本。
- 修复多次策略变更后的迁移链：`effective_pinned_hash` 从链头逐跳校验 `old_hash`，
  `apply()` 的写入 key 由当前生效 pin 派生。此前第二次迁移被忽略、第三次永久
  `MIGRATION_CONFLICT`。新增 3 次连续迁移回归。
- 如流审批卡随卡下发审批成员 uuap：审批人点击仍立即变为“已由 X 处理”并去掉按钮防重复
  点击；非审批成员在群卡片上的点击只记录“点击待校验”并保留按钮，不再对真正的审批人
  呈现假终态；网关重启后无成员列表时保持可答复。
- 区分策略拒绝与网关响应异常：`ApprovalDeliveryBlocked` 不再被写成永久
  `APPROVAL_REQUEST_RESPONSE_INVALID`；`confirm-approval-redelivery` 在作废 intent 前
  先做守卫检查，漂移中的非迁移门不会白烧一次确认配额。
- 状态可操作性：投递结果未知或运行被阻塞时不再把“打开如流审批卡”盖掉恢复指引，并展示
  `confirm-approval-redelivery` 必需的 `intent_id`；GRILL 草案仍有未答问题时状态为
  `待答题`，提示回答后以同一 job 重新提交，而不是调用必然失败的 `continue`。
- `decision_result="NO_OPEN_DECISIONS"` 携带未答 `choice_questions` 时判为不一致，
  不再允许带着未答题开 G1。
- `OPERATIONS.md` 收录 `confirm-approval-redelivery` 与 `migrate-workflow-spec` 的参数、
  精确确认字面量和前置条件。
- 本轮验证：两份独立评审合并后确认 3 个 P1 + 3 个 P2 并逐项修复，套件全量回归
  `tom-autodev` 1145 项、`tom-review` 6 项、`tom-autodebug` 通过，`node --check` 与
  `git diff --check` 通过；未据此宣称模型资格或真实平台资格。

## 2026-09-29 · 套件维护：审批材料闭环与环境 provenance

- 修复如流建群回执丢失链路：网关支持多层响应 envelope，使用已加入 `robotList` 的机器人
  身份补齐可选 `bot_id`，并将按 run intent 关联的脱敏 group receipt 持久化到 journal。
  新增只读 receipt 查询；worker 在 `RECOVERY_REQUIRED` 门禁前先核对并补写原 intent，
  不会重复建群。旧版本未产生 journal 的历史 intent 仍需明确 group ID 后受控认领，
  不能按群名猜测；新增 `adopt-group`，只按明确 group ID 核对群信息/成员后补写原 intent。
- 接通 tom-autodev 与 `/ku-doc-manage` skill：KU 入口支持环境发现和显式覆盖，自动传递
  `BAIDU_CC_USERNAME`，兼容 skill 的 debug 前缀后 JSON 响应，避免把正常 KU 查询误判为
  `KU_QUERY_UNKNOWN`。
- 修复 `run_brief` 摘要分支优先级：ProducerJob 处于待生成时不再被未来的审批 gate
  文案遮住；已批准待提交状态继续显示为 `已批准待提交`。审批等待提示同时展示当前
  `approval_id`，便于定位具体如流审批卡。相关的 3 个历史回归失败已补修并纳入验证。
- 修复协作群主题串用：群 topic/name 只从当前 iCafe requirement snapshot 派生，
  不再接受项目 profile 中残留的旧 `group_topic`；检测到已落盘 binding 与当前快照不一致时，
  `drive/continue` 在任何外部写入前 fail-closed，并给出保留审计记录后 `stop` + `start --fresh`
  的恢复路径。
- 修复 WorkerDriver 在 G0 批准后遗漏执行 `INTAKE` controller 的问题：按精确 G0 hash
  创建/复用 durable 协作群 receipt，归档 requirement snapshot，并继续驱动到 GRILL；
  `build_envelope` 对 INTAKE 使用 G0 原始 input hash，重试保持幂等。
- 修复 Infoflow 审批卡按 `target_type:target_id` 隔离 card identity；点击只显示待权限/有效期校验并保留按钮，直到账本回执确认终态；重启恢复不重复累积点击状态。
- 将 `pinned_context()` 接入实际 `_approval_context()`，G0/G4/G5/G9/G10 按当前 run、task、revision 和 input hash 读取精确材料；首次审批归档材料包，重试复用同一份内容，显式 envelope 重新计算绑定。
- BGW 环境责任边界调整为由 `tom-project-bgw` 与 iPipe 统一管理：向导移除
  `environment_provenance` 询问，profile READY 不再依赖本地 runner/image/toolchain
  证据或环境 marker 校验；保留 environment fingerprint 与远程 iPipe evidence 的绑定。
  旧 profile 中的 provenance 仅作兼容读取，不再参与确认和门禁。iPipe pipeline
  discovery 增加注入式 transport、超时和响应 schema 校验。
- 增加 `start --fresh CARD PROJECT`，在旧 run 已停止后用显式 fresh token
  创建新的审计 run；旧事件、审批和产物保留，不再通过删除状态记录实现重启。
- 当前定向控制面验证：gateway 6 项、approval materials 13 项、workspace materials 6 项、delivery 44 项、retry 20 项、progress 12 项、profile wizard 21 项、registry 20 项、schema 20 项、runtime guard 24 项通过；未据此宣称完整套件、模型资格或真实平台资格。

## 2026-09-29 · 审批可读材料、单聊按钮与通知区分

- G0–G10 / `PROFILE_REPIN` 增加对象、核对重点、应读材料与授权边界；从匹配审批 hash 的草案/固定产物提取文件和版本。自动草案及 G0 请求保存本地只读审批材料副本，不冒充已通过的阶段产物，不编造网页链接。
- 修复 worker `decision.action` 中的嵌套 G0/hash 未被投递入口读取的问题，避免仅返回 `APPROVAL_WAIT` 却不创建审批请求。
- 初始 G0 无协作群时也向成员单聊发送“同意 / 驳回”；按钮按接收目标分别记录幂等结果，未知发送结果不盲重发，降级时给出明确提示与文字回复方式。
- 按钮点击先显示“待权限和有效期校验”，不在授权前冒称处理成功；审批成员、有效期、输入 hash 和首次有效回应规则保持不变。
- IDE 停驻通知明确标为“进度通知（非审批）”，整体进度的下一步来自实际账本与当前动作，不再仅凭 `required_human_gate` 就要求用户批准。
- 定向复核补齐：首次审批材料持久化后重试复用，旧 CLI 展示只在完整送达指纹一致时兼容；显式 envelope 按当前 run/action/task/版本重算审批 hash，不信任文件自报绑定。
- 验证范围为本地控制面与网关模拟回归；不代表真实如流点击资格，也未发送 BGW-1995 审批或推进该需求。
- 当时的历史记录曾写入全量 1100 项与审批交互 233 项通过；本次维护以当前磁盘版本重新运行的定向结果为准，不将该历史数字作为现行完整回归结论。网关 Node 语法和 `git diff --check` 仍需单独验证。

## 2026-09-28 · 需求级 profile、模板向导与 Review 内联

- 项目 profile 改为按需求（卡片）隔离：`~/.tom-autodev/config/projects/<project>/<CARD>.yaml`，每个需求各存一份，并发开发的两个需求不再互相覆盖。新增 `requirement_profile_path` / `resolve_active_profile_path` / `acceptable_profile_paths`；`start` 钉住需求级 profile，运行期两处守卫（`knowledge_sync`、`_runtime_profile`）接受需求级或旧版单项目路径，其余仍报 `PROJECT_PROFILE_PATH_MISMATCH`；卡片 id 做路径穿越校验。旧的单项目 profile 保留兼容读取。
- `review_provider` 改为 schema 可选：Review 由 `tom-review` 以模型阶段产出，preflight 在缺省时跳过 review 组件，不再强依赖外部 `code-review-qa` 命令；仍支持显式配置 `source-only` 命令做兼容。
- 新增项目模板 `tom-autodev/templates/bgw.template.yaml` 与向导 `scripts/profile_wizard.py`：每个新需求只询问变化项（项目 / 知识库父目录 / 仓库与分支 / 修订 / 成员），其余从模板继承。自动推导此前易漏而变陈旧的字段——`repository`/`gitnexus` 知识源修订跟随各仓修订、`lock` 末段跟随分支、`release_rule` 的 `{primary_branch}` 跟随主业务分支；填充前若仍有 `__PLACEHOLDER__` 拒绝写入，重写同一卡片按当前 hash 守卫。
- 文档同步：`SKILL.md`、`references/setup.md`、`references/project-registry.md` 更新为需求级 profile、模板向导与 Review 内联说明。
- 验证：全量控制面回归 `1059` 项通过（新增 14 项 wizard 用例），无回归。
- 后续收紧需求启动：wizard 先展示本地 checkout 分支/HEAD 与 iPipe `ChangePipeline` 候选 ID，只有 `confirmed_inputs` 与最终 profile 完全匹配并记录 `confirmed_by` 后才允许写入；CLI 新需求启动不再静默回退到旧的项目级 profile。发现值只是候选值，不再等同于用户确认。
- 修正 checkout 来源：wizard 默认扫描当前工作目录（可用 `--workspace-root` 明确指定），模板使用 `__WORKSPACE_ROOT__` 展开仓库路径；不再读取模板中写死的 `/Users/.../bgw` 目录。候选列表同时展示当前目录下所有 Git checkout 的路径、分支和 HEAD，仓目录选择也进入确认内容。

## 2026-09-28 · 审批投递失败闭合与资格证据

- 审批投递结果改为 fail-closed：真实 `DELIVERY_FAILED` 回执不会再被误判为 `PARKED / APPROVAL_WAIT`；合同形状的 `PENDING + approval_id` 正常等待路径保留。
- 独立 C 修复后复核关闭 `IND-C-001/P1`；生产形状 CLI 回归返回 `ok=false`、`APPROVAL_DELIVERY_FAILED` 和非零退出。
- M v3 的 9 个场景在 3 个独立上下文各运行一次，三批均 9/9、`fail_if=0`、合同/语义校验通过；精确模型身份在当前执行环境不可见，故形式化 M 结论记录为 `INCOMPLETE`，不宣称跨模型资格。
- 验证：tom-autodev 1045 项、tom-review 6 项、tom-diagnose 回归、Python 编译、Node 语法和 `git diff --check` 全部通过。

## 2026-09-27 · 工作卡进度收口修复

- 工作卡新增"流程"行后同步更新回归：模块/待处理断言的行位改到插入后的位置，发布快照回归补齐 release checkpoint 的 `build_id`，与当前 build 身份匹配对齐。
- 工作卡正文压到 7 行、`run_id` 收进标题，在网关 8 组件上限下留出一行余量，避免后续新增行被静默截断。
- `_current_release_identities` 在发布 build 解析器临时不可用时回退到流水线 checkpoint 身份，而不是直接清空——已核验的发布进度不再被静默清零；发布 checkpoint 自带 `build_id/binding_hash` 仍做防陈旧校验。
- 验证：全量控制面回归 `1043` 项通过（修复前有 3 项转红）。

## 2026-09-26 · 工作卡一致性与通知进度收口

- 工作卡成功去重只认有效卡片回执；放弃的未知 intent 不会被误判为已刷新。
- 人工核对回执要求记录核对人和理由，并严格校验 `work-<run_id>` 卡片身份；2xx 但网关返回业务失败时保留未知结果。
- 发布进度按当前冻结计划的 build 与 binding 计数，旧 build 的发布记录不能覆盖新 build。
- 工作卡刷新补充每次 intent 对应的 `cardInstanceId`，并区分发送前拒绝与远端结果未知；新增 `work-card reconcile` 人工核对/放弃入口。
- 审批初始消息、提醒、确认/超时、iPipe watcher 和动态工作卡统一展示阶段路线与整体进度；iPipe watcher 也会刷新工作卡。
- Comate 本地审批回执同时携带可直接展示的 `progress_text`，与如流审批消息使用同一份快照。
- 协作失败路由也追加同一份整体进度，并保持通知幂等键不受展示快照变化影响。

## 2026-09-25 · 增量前沿与 iPipe 观察优化

- DAG amend 改为按 task scope 判断 Review 是否仍覆盖当前任务：节点语义和直接前驱未变化的 task 继续复用 Review，变化 task 才重新进入 frontier；缺少 Review 时刻 DAG 证据时 fail-closed。补充增量 amend、受影响 task 重开和 frontier 不清空回归。
- `status` 使用只读 next oracle，不会因为刷新状态隐式恢复 SUBMIT 或推进 run；对旧版只实现 `next(run_id)` 的轻量 adapter 保留兼容。
- `submit-draft` 接入 run 级 worker lease，与 `advance` 串行保护同一 run 的 frontier、ProducerJob 和 phase completion。
- iPipe worker 和 watcher 改用 bounded `monitor_once()`；长时间运行的 build 不再让一次 drive 或 watcher tick 持续占用 lease。旧的连续 `monitor()` 接口保留兼容。
- 新增 SQLite iPipe monitoring checkpoint，按 `(run_id, build_id)` 保存最近状态、stage、deadline、证据引用和更新时间；`status` 可展示活动 stage 与最近检查时间。checkpoint 只是观察事实，不替代 iPipe evidence ingest，也不自动推进 phase。
- 验证：受影响回归 `183` 项通过；完整控制面回归 `1031` 项通过。测试输出仍有少量 ResourceWarning，未影响测试结果，列入后续清理项。

## 2026-09-26 · 统一多仓进度快照

- 新增只读 `ProgressSnapshot`，从既有 workflow、DAG、冻结 pipeline plan、iPipe checkpoint 和发布证据汇总阶段、任务、代码库/模块、流水线及 release 进度。
- `status`、审批续跑/IDE 停靠通知和 iPipe 成功/失败/人工等待通知复用同一份快照；保留原有幂等、审批 hash、worker lease 和 evidence ingest 边界。
- iPipe checkpoint 补充 module/build identity，避免多仓通知把不同模块合并成一个不确定状态。
- 第一阶段不引入动态工作卡更新，也不改变多 worker 并行策略。

## 2026-09-26 · 收紧 Review 到提交的证据闭环

- `pipeline_plan.current_descriptors()` 现在要求带 Review 绑定的 PASS change-set 同时具备有效的当前 Review，且 Review 的 `change_set_hash` 必须匹配 descriptor 的 `candidate_hash`。
- Review 缺失、损坏、错绑 candidate 或 archive/index 中断时，descriptor 不再进入 submission 和 pipeline plan；未带 Review 绑定的历史 descriptor 继续保留兼容读取。
- 新增缺失 Review 和 candidate hash 错配回归，验证提交前 fail-closed。

## 2026-09-26 · 补齐 RELEASE_WAITING 观察闭环

- iPipe watcher 同时观察 `IPIPE` 和 `RELEASE`；进入发布阶段后按冻结 pipeline plan 和 run-owned build 逐模块只读核验发布状态。
- 平台尚未发布时发送幂等 `RELEASE_WAITING` 通知，包含模块、build、发布规则和统一 `ProgressSnapshot`；watcher 会继续检查，不重新申请 G9，也不重复触发 iPipe。
- 发布出现后发送一次模块成功通知；发布观察 checkpoint 与编译观察 checkpoint 分开保存，避免覆盖多仓 build 的最新状态。
- 新增发布等待、重启幂等和发布完成通知回归；受影响控制面测试 80 项通过，watcher 测试 11 项通过。

## 2026-09-26 · 如流动态工作卡

- 新增固定 `work-<run_id>` 身份的无按钮工作卡；审批 watcher 以统一进度快照刷新群内同一张卡，覆盖任务、多仓模块、流水线、发布和下一步。
- 通过持久化外部 intent 与 receipt 去重；未知投递结果阻止后续刷新并返回 `WORK_CARD_QUERY_REQUIRED`，避免跨重启重复发送。
- 工作卡是只读展示，审批卡与 iPipe 通知仍保留原有独立语义；不自动推进阶段、不写业务发布证据。
- 发布进度区分流水线成功与平台发布核验：只有 release checkpoint 成功或 run 达到 `RELEASE_SUCCESS` 才计入发布模块完成数。
- 工作卡刷新同时接入审批 watcher 和 iPipe watcher，两个观察进程通过同一份 intent/receipt 账本协调，不因 watcher 重叠而重复渲染。
- 阶段路线按当前事件的 predecessor 链展示；诊断回退会收起失效的下游分支，`RELEASE_SUCCESS` 才统一显示全路线完成。

## 2026-09-24 · 日常入口第二轮优化

- `process CARD PROJECT` 合并创建和驱动，复用唯一活动 run；项目不匹配、多候选和终态历史均给出明确结果，不静默切换或重启。
- `submit-draft` 自动发起绑定保存草案的审批，成功返回 `PARKED / APPROVAL_WAIT`；投递失败保留草案和错误，`continue` 可重试。人工审批与 hash 校验保留。
- `context TARGET` 只读展示当前任务的固定输入引用、版本、schema 和 ProducerJob 状态，包含已保存草案标记；跳过恢复动作和动作缓存写入，不输出草案正文。
- `status` 区分“已完成”和“已停止”，终态不再展示历史审批为当前待办；worker 将正常终态识别为 `TERMINAL`。摘要补充 job/schema/草案状态，已批准草案提示继续推进。
- `preflight` 补充 Stop-hook 注册与 watcher 命令文件检查；缺少统一心跳时，调度、运行状态、最近成功时间明确为 `unknown`。说明 Stop-hook 无法唤醒已经结束的会话。
- 同步 README、操作参考、父 skill 和 Producer 合同。本轮只复核以上控制面变更；未进行真实内网平台交付或模型资格验收。

## 2026-09-24

- 精简 KU/iCafe 协作写入范围（控制策略变更，改变 `workflow_spec.canonical_hash()`）：`SPEC` 由 `both` 降为 `ku_only`（去掉 iCafe 里程碑评论，保留 KU 文档），`REVIEW` 与 `DIAGNOSE` 由 `ku_only` 降为 `skip`（KU、iCafe 都不写）。`INTAKE`（KU 根锚点）、`RELEASE`、`IPIPE` 及其余阶段不变。阶段产物一律仍存本地 `artifact_store`，正确性与恢复不读 KU，故本次仅减少人可见的协作写入。同步更新 `workflow_spec` 内注释与 `docs/OPERATIONS.md` 的发布范围表。
- 影响：`knowledge_scope` 属于 `canonical_hash()` 的一部分，本次编辑改变工作流规则 hash——已 pin 旧 hash 的在途 run 会被 `WORKFLOW_SPEC_DRIFT` 阻断，需在旧版本跑完或在新版本重启并重新审批（`WORKFLOW_VERSION` 标签沿用 `workflow-spec-v2`，与既往策略微调惯例一致，精确身份以 hash 为准）。
- 验证：`test_workflow_spec` 29 项、`test_fake_e2e` 71 项（其按 `knowledge_scope` 动态断言每阶段 KU/iCafe 写入）、`test_phase_protocol` 27 项、`test_knowledge_sync` 19 项通过；随后跑完整 C 命令。

## 2026-09-22

按 `TAD-REVIEW-EXIT 1.0` 首次有界工程评审修复（初审基线 `273d2507bcfb600d1a1ab9e5e6d8f61b72d41565`）：

- 关闭通用 `advance` 绕过发布证据进入 `RELEASE_SUCCESS` 的路径，返回 `RELEASE_INGEST_REQUIRED`；成功终态只能由完整冻结计划、全部模块及平台核验回执的摄取路径写入。
- 补齐 `Gx#retry-N` 在 worker、EvidenceGate、G0/G10、watcher 与运行摘要中的消费；重发次数保持有界，run/gate/hash/成员与内容版本校验保留。审批台账返回真实 `created_at`，使重发状态与批准后的续跑提示一致。
- 允许 `DIAGNOSE` 提交 `repair_diff_hash=null` 的 REPAIR 提案；G6 只授权诊断与方向，实际候选仍经 PLAN/IMPLEMENT 和新的 G5。源码 Review 诊断使用 null 流水线身份，iPipe 诊断严格绑定真实 build、环境、失败签名及 stage/job。
- iPipe runtime 保存独立 profile 副本，并在新操作/证据写入前核对磁盘文件与最新批准 pin；缓存旧对象、原字典突变和轮询期间漂移均不能继续写入。已完成 trigger/rerun 的准确回执仍可只读重放，合法 re-pin 后须重建 runtime。
- 第二轮沿用 A-05 修复 iCode 相邻路径：factory 必须加载批准的 profile 和提交策略，controller 在调用提交 adapter 前检查配置；runtime 在预检、远端查询、推送及写回执边界再次检查。旧对象在 re-pin 后失效；推送后出现漂移保留 pending intent，不生成成功回执或盲目重发。
- 同步 Diagnose 合同、README、操作参考与整合报告；固定行为场景提升为 v2，撤销已经解决的无 diff 阻断预期。控制面回归不等于 v2 模型资格或真实内网平台验收。
- 第一轮验证：控制面 `961` 项通过（相比初审基线新增 `50` 项），Review `6` 项通过，Diagnose 隔离 shell 回归通过；修改文档的 `33` 个本地链接/锚点、v2 场景 JSON 与 `git diff --check` 通过。新增回归已对原始基线验证能暴露发布、诊断、审批和缓存配置漂移问题；并发补证使用真实 worker、临时 Git/SQLite，验证同 run 租约和两 run 共享源仓的所有权隔离。
- 第二轮验证：控制面 `976` 项通过（新增 iCode profile 回归 `15` 项），Review `6` 项和 Diagnose 隔离 shell 回归再次通过。两轮预算用尽后按标准记录修复及证据缺口，测试全绿不提升为完整 C/M/P 资格。
- 本批次已确认的五类缺陷触发修复并通过定向复核；全量 C 结论仍为 `INCOMPLETE`，缺少 WF-08 其余可写入口的 profile 等价守卫与只读回放证据。M/P 均未验证；两轮后停止开放式评审，剩余入口矩阵转为明确取证任务。

套件评审收敛规则：

- 新增唯一标准 `docs/WORKFLOW_EXIT_CRITERIA.md`：固定版本/代码基线、十项行为矩阵、缺陷证据与分级、分层验收、风险台账、报告模板，以及有界修复复核和停止/重开条件。
- 新增仓库 `AGENTS.md` 与 Claude/Gemini 入口指针；`tom-autodev`、`tom-review` 仅在评审套件自身时读取该标准，业务 Review JSON 与控制面运行合同不变。
- README 增加统一评审用法，明确文档规则尚非 CI 强制门禁；同步修复预算已接线的历史状态。此变更不声明当前套件已通过控制面、跨模型或真实平台验收。
- 验证：涉及文件的 69 个本地链接/锚点、两个修改 skill 的 frontmatter 与安装链接路径检查通过；本次仅改文档与入口指针，未重跑控制面回归或执行真实平台操作。

外部第二轮(Grok)复核的七项经逐条独立核验后,修复其中四项确认为真且可控的问题(其余三项:工作区基线无环境要求为良性、KU 发布顺序为误报、worker 租约未接生产且被幂等层兜底,均未改):

- 审批重发门卡死:超时/不可投递的审批以 `G7#retry-N` 重发保持台账 `(run_id, action, input_hash)` 唯一,但所有门消费方按裸门号精确比较,重发获批后 `!= "G7"` 永久 `APPROVAL_GATE_MISMATCH`。新增共享 `approval_ledger.gate_of()`,在 icode/ipipe `_approved_record`、`phase_protocol` 两处控制器审批检查、`orchestrator` 的 G7 提交与恢复扫描共 6 处统一按裸门号比较;自动重发加 `MAX_APPROVAL_RETRIES` 上限,超限返回 `APPROVAL_RETRY_EXHAUSTED` 而非无限 `#retry-N`。
- 单 run 修复预算未接:生产 DIAGNOSE 路由只用跨 run 的 `known_failure_verdict`(单 run `distinct_runs` 恒为 1,永不触发),`repair_policy.next_action` 的每-run 上限从未接线,模型每轮 `route=REPAIR` 可无限循环(含环境类不可修复失败)。`_completion_target` 接受 REPAIR 前,用从持久化 DIAGNOSE 制品重建的本-run 修复历史(证据充分性、失败签名、无进展、未解决,均由控制器判定,不信模型 attempt 计数)喂 `next_action`,STOP→STOPPED、三次未解决→ARCHITECTURE_REVIEW。
- EvidenceGate 指纹只查存在不查一致:通用 `advance` 路径不经冻结计划绑定,持 G9 权者可传两个相同但过期的环境指纹通过 RELEASE。`_ledger_backed_evidence` 对必填环境的 action 用当前 pinned profile 的规范环境指纹覆盖调用方的"期望"侧,证据须匹配真实固定环境;profile 读不到则留空、fail-closed 为缺证据。
- Change-Id 子串检测与推送前权威检查不一致:`submit_descriptor` 用 `"Change-Id:" not in body` 子串判定,正文散文里的同名文字会被当作已带 trailer,与 `icode_runtime._commit_change_id` 的行首锚定不一致,使无 trailer 提交延迟到 push 才非可重试地失败。改用共享 `_has_change_id_trailer` 行首锚定,与权威读者对齐。
- 验证:控制面 `911` 项通过(本轮新增 `9` 项:门号归一+重发上限、发布环境指纹钉定、单 run 修复预算)。未触发真实内网业务构建或发布。

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
