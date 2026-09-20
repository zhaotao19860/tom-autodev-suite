# Changelog

All notable changes to this suite. Dates are the dates the work landed locally; entries
before 2026-08-27 are reconstructed from design documents and file timestamps, since the
suite had no version control before then.

## 2026-09-20

Codex 评审全量修复（分步进行；每步全绿后推进）：

- 上一轮评审补丁：拆超长变更行；`transition_policy.failure_target` 改为委托 `workflow_spec.failure_target`（单一事实源）并移除未用的 `current` 参数；worker best-effort 异常改为记日志；`_gate_settled` 要求具体 input_hash（None 不再匹配任意审批）
- HIGH-002：ProducerJob 草案先校验 schema 再 fulfill——schema 非法草案返回 `DRAFT_SCHEMA_INVALID`（retry_allowed）且不锁定 job，可用修正版重试同一 frontier，不再变成不可恢复的 `PRODUCER_JOB_CONFLICT`；receipt 的 validators_passed 只记真跑过的校验；merged SPEC+TASKS 同样先校验两半再落地
- HIGH-001：审批后 worker 自动消费持久化草案——一次草案 `submit_draft` 记录后，批准其闸门，下一次 `advance()`/`resume()` 直接从持久化草案完成该相位，**不再需要二次 `submit_draft`、不重新唤醒 producer**（提取 `_produce` 共享路径；auto-consume 走同一校验+审批绑定，action_id 键防旧草案越过新审批）
- MEDIUM-002：merged SPEC+TASKS 崩溃可恢复——SPEC 提交后把 DAG 以 checkpoint 落到 TASKS 的 ProducerJob，若在 TASKS 提交前崩溃，`advance()` 从持久化 DAG 恢复 TASKS（复用 HIGH-001），SPEC 不再孤儿、不重跑 producer
- 控制面回归测试 `779` 项全部通过

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
