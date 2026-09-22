# 2026-09-22 五项修复定向复核：第 1 轮

**本轮结论：C 仍为 BLOCKED。** A-01/B-01 至 A-04/B-04 的原始触发已修复，A-05 的 iPipe 触发已修复；但在同一 WF-08 影响面确认 iCode 提交仍能在 profile 漂移后发出写操作。此项沿用 A-05，不新开全仓初审。必要的全入口 profile 证据也尚未齐全，不能由回归数量推导完整 C 通过。

## 基线与边界

- 标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库：`/Users/tom/Desktop/skills/tom-autodev-suite`；分支：`phase1-workflowspec`。
- base_commit 与工作区锚点 HEAD：`273d2507bcfb600d1a1ab9e5e6d8f61b72d41565`。**本轮修复尚未提交，因此没有可承载正式 PASS 的 target_commit。** 下面结论只绑定固定工作区内容。
- 27 个变更文件清单指纹：`e6635bace347994af1de62b09da5a57d16f6c2512d833c1c5a7918d877c28142`。算法为按路径排序，每项拼接 `SHA256(file) + 两空格 + 相对路径 + LF` 后再 SHA-256。完整逐文件 hash、原始相邻 iCode 调用方和复现 fixture hash 见 [round1-manifest.json](evidence/2026-09-22-round1-manifest.json)。
- 在指纹一致时复制的只读取证快照：`/var/folders/y8/t5r8bw1d4f79vn_9f4hzl_tw0000gn/T/tad-targeted-b-round1-qajiqk_1`。复制后再次验证上述 27 项一致。后续第 2 轮修改不影响本轮证据。
- workflow：`workflow-spec-v2`；规范 hash：`71f51bcabcd042cf3870e78e57568b0940e0288734c4be4b14a6398aaffb71c6`，与初审相同。
- 16 个 schema、12 个 SKILL.md 联合清单 hash：`441cdc30089dd033651d4b2b9dda5adc8145bc977ed5b3699d7544910865c30c`。算法同初审 B：按路径排序，拼接 `path + NUL + SHA256(file).digest()`。变化来自 diagnosis schema；具体 skill 未改，Diagnose 相邻 phase-contract 已更新并列入 27 项。
- profile：只使用冻结代码中的本地 fixture；未运行真实业务 profile。模型行为场景更新为 v2（文件 hash 见 manifest），没有 M 层模型原始响应。
- 执行环境：macOS 26.6.2 arm64 / Python 3.9.6；临时 Git、SQLite、mock 平台/CLI。不执行真实 iCode/iPipe、通知或发布，不构建业务工程。
- 评审者：Codex 独立上下文 B；具体模型版本未知；日期 2026-09-22。负责人为根任务维护者 `/root`，剩余风险接受由用户决定，本复核未代为接受。
- 预算：本批次最多两轮修复→复核；本文件为第 1 轮。根任务明确指定仅检查五项已确认修复及受影响调用方，随后补充指定核验 A-05 相邻 iCode 路径；不扩展无关能力，不派生 agent，不修改生产代码，不提交。
- 继承：[初审 A](2026-09-22-initial-a.md)、[初审 B](2026-09-22-initial-b.md) 的相同标准/原始 commit 证据；两份初审已独立完成后才相互读取。保留原始失败判定，修复状态以本轮逐项证据更新。

初审快照中的 package-lock 差异仍是历史事实。根任务报告在本轮主仓完整测试前后核对该文件与原始基线均无差异；本轮快照也继承主仓字节，不将旧快照差异归因给某个执行者，也不把主仓全绿倒写成初审快照完全一致。

## 验收矩阵与继承

| WF | 本轮影响、证据与继承理由 | 本轮状态 |
|---|---|---|
| WF-01 | 通用 advance 新增终态拒绝，发布回归确认不写事件/产物；诊断在 draft 锁定前校验真实来源。原始 producer/task/revision/前驱证据在未改路径继承。 | 本轮修复路径通过；非全量重验 |
| WF-02 | retry 只规范化 base gate，保留 run/hash/decision/成员/内容 hash；真实 SQLite、worker 重启、G0/G10、watcher 重发上限均有正反例。 | A-02/B-02 关闭原始触发；profile 旧授权仍见 A-05 |
| WF-03 | 本轮未改 workspace/ownership/revision 逻辑；继承初审，并由 WF-05 同时断言脏源仓用户内容未变。 | 已列场景证据有效 |
| WF-04 | 保存草案及重发审批重启可消费；iPipe 已完成回执只读重放、未知 intent 不借重放继续写入；其他 checkpoint 路径继承。 | 本轮回归通过；A-05 的 iCode 写入不能视为安全恢复 |
| WF-05 | [四项补证](2026-09-22-wf05-supplement.md)：同 run 两 worker、不同 run 共享源仓资源锁、死租约接管、重放、locks=None 顺序边界；在原始生产快照和本轮工作区均通过。 | 本地受支持 API/锁合同已补证；真实 Comate 宿主/跨主机锁未验证 |
| WF-06 | pipeline-plan 正常多模块、依赖、共享测试变化、独立复用和版本失效回归重新通过；核心计划逻辑未修改，其余初审证据继承。 | 已列场景通过 |
| WF-07 | 精确 G9/同 revisions/正确环境/伪 artifact 名称不能使 advance 成功；真实平台 mock proof 齐全后正常 ingestion 成功。缺模块和 verifier 不匹配既有回归通过。 | A-01/B-01 关闭原始触发 |
| WF-08 | iPipe cache/磁盘/repin/I/O 后写入边界及只读回放 24 项通过；实际相邻 iCode 提交仍先写后校验。初审未覆盖的其他 profile 可写入口没有因此获得等价证明。 | **BLOCKED：A-05 未关闭**；全入口必要证据仍 INCOMPLETE |
| WF-09 | patchless REPAIR、Review→DIAGNOSE null identity、iPipe 真实身份与修正草案回归通过；预算/签名持久化逻辑未改，继承初审证据。 | A-03/B-03、A-04/B-04 关闭原始触发 |
| WF-10 | 不再通过 generic advance 生成无发布证据成功；receipt/cache/secret 其余原始证据继承。测试均标明模拟，不推导业务或模型资格。 | 本轮发布修复通过；iCode 旧授权仍见 A-05 |

## 五项 finding 的定向处置

| finding | 修复、相邻调用方与反证核对 | 处置 |
|---|---|---|
| A-01 / B-01，P1 | `orchestrator.py:1115` 对新增 `RELEASE_SUCCESS` 推进返回 `RELEASE_INGEST_REQUIRED`，早于 EvidenceGate 和状态提交；CLI 委托同入口。`test_generic_advance_cannot_claim_release_success_without_platform_proofs` 使用真实 Orchestrator/准确批准，证明事件与 RELEASE 产物不变、无平台 release；随后 A/B 发布 proof 齐全时 `_release()` 到达成功。既有 all-module、错 build/verifier 与正常 ingestion 回归重新运行。早先已保存的只读回执路径未改，本修复不声称审计/迁移已有历史成功记录。 | 原始触发已修复，正常摄取未回退 |
| A-02 / B-02，P1 | worker `_gate_settled`/`_settled_approval_id`、EvidenceGate、G0 collaboration、G10 summary 和 watcher/brief 使用已有 `gate_of`；真实 ledger 的 `for_run` 过滤与消费者准确 hash/decision 校验保留。新增 `created_at` 是实际持久字段，不是人为估时。测试从原卡 TIMEOUT 或 delivery failure 经正式 reissue，再批准；验证重启消费同草案、内容变化拒绝、错 run/gate/hash/成员和 REJECT/pending 拒绝、watcher 三次上限、G0 成员绑定及 G10 拒绝回放。 | 原始触发及本轮相关消费者已修复 |
| A-03 / B-03，P1 | `_validate_diagnosis` 不再要求 REPAIR 已有 diff；充分证据仍要 hypothesis/方向/计划，非 REPAIR 不接受 diff。真实 worker 在 null hash 下先停 G6；批准后新 Orchestrator 消费保存草案到 PLAN/G4，未生成 PLAN 之前的补丁。未改 IMPLEMENT 的候选 hash/G5 绑定。 | 原始无 diff 闭环已修复 |
| A-04 / B-04，P1 | diagnosis schema 允许执行身份 null；`PhaseProtocol.validate_result` 仍绑定 task/前驱/frozen revisions，随后 `_diagnosis_source_matches` 对 Review 强制四项 null，对 iPipe 精确匹配 build/env/signature，stage/job 须属于真实前驱。完整 Review 拒绝→DIAGNOSE→G6→PLAN 正常路径通过；借用 pipeline ID、抹去 build、换 env/stage/job/signature 在 producer job 锁定前拒绝且可修正。无 stage/job 仅在 source 对应列表不存在时允许。 | 原始 Review 身份合同已修复，iPipe 身份没有因 schema 放宽而擦除 |
| A-05，P1 | iPipe 修复及直接调用方详见下节；第 1 轮已覆盖原始 cached runtime 写入触发。相邻 iCode 入口经真实工厂仍没有 profile 守卫，且 controller 把检查放在 submit 之后；定向复现确认实际写副作用。 | **未关闭；同根因扩展触发确认** |

### A-05：iPipe 修复的有效范围

核对 worker `_execute_ipipe`、`_execute_release`、CLI runtime 工厂、IpipeClient 转发与 IpipeWatch 调用顺序，均使用同一个受管 runtime。新 runtime 深拷贝已校验 profile，保存构造 pin；新增副作用前检查 intake 磁盘 hash、最新批准 repin 和对象内容，远端查询/日志读取/轮询后在实际写边界重查。

24 项专用测试比较 SQLite dump 与 fake API 调用，覆盖 discover/trigger/rerun/monitor/planned release、缺文件、调用方或公开缓存字典变化、连续两次合法 repin、查询/日志/轮询期间漂移。旧实例失效，新工厂实例可用。已完成且准确绑定的 trigger/rerun 回执在漂移后不新增 DB/API 调用；未知 intent 的续收回执被拒绝。release 纯查询仍只读。原始 workflow guard 保持不变；这不是跨文件/SQLite/平台的原子事务承诺。

无 `profile_path` 的直接构造历史 fixture 只保留内存绑定语义；真实工厂要求完整磁盘配置。这些测试证明 iPipe 范围，不能推广为 iCode、长期缓存 KnowledgeSync 或所有维护入口已有相同 profile 守卫。

### A-05 扩展触发：iCode 先写平台、后报告 profile 冲突

- 违反合同：WF-08 明确要求 profile 变化在可写入口副作用前阻断；旧 G7 不能继续用于不相容配置。
- 前置：正常 pinned run 处于 SUBMIT，真实临时 Git 拥有 worktree 与已审候选（含子提交），准确 G7 已在真实 ApprovalLedger 批准，runtime 通过 `Orchestrator.icode_runtime` 构造。磁盘 `environment_profile.image_digest` 随后改变。
- 在调用前，真实 `_runtime_profile(run)` 已返回 `PROFILE_CONFLICT`，排除“实际上没有发生漂移”。
- 入口一：`Orchestrator.submit_to_ipipe` 调用真实 `IcodeRuntime.submit`；模拟 `push_cr` 一次并写入 OK iCode receipt 后，外层才返回 `PROFILE_CONFLICT`。run 留在 SUBMIT 不代表无副作用。
- 入口二：直接调用同一正式工厂构造的 `runtime.submit`，模拟 `push_cr` 一次、写 OK receipt，并直接返回 OK。
- 当前代码路径：`orchestrator.py:1367` 工厂只查 run/options；`submit_to_ipipe:1422` 只经过 workflow guard；`_submit_to_ipipe:1462` 先 submit，直到 `:1515` 才校验 profile。`icode_runtime.py:122` 的装饰器仅查 workflow，`:178` claim intent，`:221` 发 `push_cr`，`:573` 写 receipt。
- 反证核对：旧 G7 精确 hash、持久 review artifact、工作区所有权、commit/branch/remote 与 CR 身份验证都通过，说明这是受支持路径中的 profile 守卫遗漏。标准 CLI helper 在构造 runtime 前调用 `_runtime_profile`，可挡住该高层入口已存在的漂移；但本轮范围明确包括可直接调用的维护/runtime 边界，缓存 runtime 和外层 `submit_to_ipipe` 仍可达，且高层检查不替代 runtime 在后续 I/O 边界的检查。
- 严重级别/处置：P1，确认，合并至 A-05 而不是另建同根 finding。根任务已收到复现并负责第 2 轮有界修复；没有风险接受。

可重复脚本完整保存为 [repro.py.txt](evidence/2026-09-22-a05-icode-profile-repro.py.txt)。运行时 Python 可直接执行 `.txt`，参数应为本轮快照或待复核仓库；脚本仅创建临时 Git/SQLite、本地 bare remote 和 `FakeArgvTransport`，没有网络平台调用。

```text
$ PYTHONDONTWRITEBYTECODE=1 python3 docs/reviews/evidence/2026-09-22-a05-icode-profile-repro.py.txt <固定快照或仓库路径>
Orchestrator.submit_to_ipipe:
  profile_guard_before_call=PROFILE_CONFLICT
  mock_push_cr_count=1, persisted_icode_receipt=true, persisted_receipt_reason=OK
  result_ok=false, result_reason=PROFILE_CONFLICT, run_state=SUBMIT
IcodeRuntime.submit (factory-built):
  profile_guard_before_call=PROFILE_CONFLICT
  mock_push_cr_count=1, persisted_icode_receipt=true, persisted_receipt_reason=OK
  result_ok=true, result_reason=OK, run_state=SUBMIT
```

完整原始 JSON 输出见 [repro-output.txt](evidence/2026-09-22-a05-icode-repro-output.txt)。主仓初次执行与冻结快照执行结果一致；没有更改生产文件来构造触发。

## 实际验证记录

B 在本轮冻结快照中用 unittest loader 只运行四个受影响模块，原始逐用例输出见 [round1-tests.txt](evidence/2026-09-22-round1-tests.txt)：

```text
test_pipeline_plan               25 tests
test_approval_gate_retry         20 tests
test_diagnosis_contract           5 tests
test_runtime_profile_guard      24 tests
----------------------------------------------------------------------
Ran 74 tests in 9.305s

OK
```

等价命令为在冻结快照 `tom-autodev/scripts/tests` 下执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_pipeline_plan test_approval_gate_retry test_diagnosis_contract test_runtime_profile_guard
```

根任务另报告主仓完整控制面 `961 tests / 116.666s / OK`、tom-review `6 tests / OK`、Diagnose `PASS`、`git diff --check` 通过；测试收集包含 B 新增的四个 WF-05 用例。B 没有重复完整测试；独立定向回归与反例是本轮结论依据。曾误在无 `.git` 的快照运行 git diff，返回 exit 129，未把该命令计为通过；有效差分/链接检查在主仓执行并另记。

归档后，B 在主仓执行 `git diff --check`，exit 0；核对初审 B、WF-05 补证和本报告的 10 个本地链接/锚点，全部可解析且无尾部空白；重新计算冻结快照中 27 个变更文件和 5 个相邻依赖的内容 hash，共 32 项全部匹配，清单总指纹也匹配。

## 剩余风险与下一步

| ID | 范围/影响 | 处置、负责人 | 停止/重开条件 |
|---|---|---|---|
| A-05 | iCode direct/controller 提交在 profile 漂移后仍 push/写成功回执 | `/root` 第 2 轮仅修此确认路径及其副作用/回放边界；本轮不改生产代码 | 固定第二轮内容，原反例不再产生写入，正常提交和准确只读回放有证据后定向复核 |
| GAP-WF08 | 未建立所有声明可写入口的 profile 守卫与纯回放等价矩阵 | 必要事实未查明记 INCOMPLETE；本轮不以抽样全绿填满矩阵 | 负责人列明必要入口、已有静态/动态证据和仍缺项；不自动扩大全仓扫描 |
| FINAL-COMMIT | 第 1 轮仅有工作区指纹，尚无正式 target_commit | 根任务提交后核对最终文件与本轮/第二轮 manifest 差异 | 内容匹配才可继承，不凭分支名继承 |
| HOST/P | 真实 Comate 宿主锁注入、跨主机共享文件系统、真实平台未验证 | 本地 C 合同范围见 WF-05 补证；不宣称实际部署通过 | 真实集成/部署另行获得相应证据 |
| M | v2 场景未进行准确模型身份的 8×3 资格测试 | 未验证；旧 v1 单轮结果不继承为 v2 PASS | 按固定场景和准确模型/skill/controller 身份取证 |

没有新增 P2/P3，也没有把测试数量、模型一致意见或历史披露当作风险接受。

## 本轮结论

- C：**BLOCKED**，A-05 存在确认的相邻 iCode P1 残留；同时保留 `GAP-WF08` 与最终 commit 证据缺口。
- M：**未验证**。
- P：**未验证**。
- 局部复核：四个原始问题及 iPipe 原始触发已得到正反例/正常路径证据；A-05 整项尚不能关闭。
- 第 1 轮到此停止，不重扫无关能力。后续只在根任务明确指定的第二轮范围复核；若预算用尽仍有阻断/必要缺证据，按标准记录 BLOCKED/INCOMPLETE，不因预算而放行。
