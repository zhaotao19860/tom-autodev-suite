# 2026-09-22 A-05 第二轮最终定向复核

**A-05 本批次已确认的 iCode/iPipe 触发已修复，第二轮局部复核通过。完整 C 仍为 INCOMPLETE。** 没有发现新的确认 P0/P1/P2；其他 profile 可写入口与纯回放的必要证据仍未齐全，按 [GAP-WF08 后续取证任务](2026-09-22-profile-guard-followup.md) 保留，不把用例全绿写成全入口等价证明。第 2 轮到此停止，不自动启动第 3 轮。

## 固定身份与范围

- 标准：`TAD-REVIEW-EXIT / 1.0`；标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库：`/Users/tom/Desktop/skills/tom-autodev-suite`；分支：`phase1-workflowspec`。
- base_commit/工作区锚点：`273d2507bcfb600d1a1ab9e5e6d8f61b72d41565`。本次是**未提交工作区预审**，尚无正式 target_commit；提交后需由根任务核对最终 SHA 和内容清单。
- 提交后绑定：根任务随后提交实现为 **target_commit `daa76f6fee3f8d609943666d3e2b9f456026b2fa`**。B 逐一读取此 commit 的四项修复文件和最终清单的 304 个 Git blob，SHA-256 全部匹配已测试的固定内容；不重跑内容未变的测试。前述工作区预审记录保留其执行时点，本报告最终结论现绑定该准确提交。
- 第二轮四文件清单：[round2-code-manifest.json](evidence/2026-09-22-round2-code-manifest.json)，总指纹 `f6ac581f90baee7112697857a7947c1d210a078de8371c5f5e1ca4046b2bceb6`。算法为按清单路径顺序拼接 `path + NUL + SHA256(file).digest()`，再 SHA-256。
- 冻结快照：`/var/folders/y8/t5r8bw1d4f79vn_9f4hzl_tw0000gn/T/tad-targeted-b-round2-ar0mbgqf`。B 在复制前后及报告前核对四项 hash；也核对根任务 [304 文件最终测试清单](evidence/2026-09-22-final-tested-manifest.json)，主仓与此快照全部一致，总指纹 `247f229f9daeda8dd8b2eb6ab85cd1a3a17fd5f56dd34f0d852609ffc86217bb`。
- workflow：`workflow-spec-v2`，规范 hash `71f51bcabcd042cf3870e78e57568b0940e0288734c4be4b14a6398aaffb71c6`；16 schema/12 SKILL 联合 hash `441cdc30089dd033651d4b2b9dda5adc8145bc977ed5b3699d7544910865c30c`。均与第 1 轮相同；v2 模型场景 hash 为 `6c81ea0de6f616e0b20863a0c23b33f61ef0cc640e2faf582220a535993d4d15`，没有执行 M 资格测试。
- 支持环境的实际取证：macOS 26.6.2 arm64 / Python 3.9.6；真实临时 Git、SQLite、控制器和 runtime；本地 bare remote、模拟 CLI/平台。没有真实平台调用、发送通知、业务编译或发布。
- 评审者：Codex 独立上下文 B，准确模型版本未知；日期 2026-09-22。负责人为根任务维护者 `/root`；剩余缺口负责人见后续任务，未代为接受风险。
- 预算：本批次允许最多两轮修复→复核，本次为第 2 轮且最后一轮。只检查第二轮 iCode factory、controller 前置检查、cached runtime 写入/回执边界及相邻调用方；只写本报告和证据，未改冻结代码、未提交、未派生 agent。
- 继承：[初审 A](2026-09-22-initial-a.md)、[初审 B](2026-09-22-initial-b.md)、[第 1 轮定向复核](2026-09-22-targeted-review.md) 和 [WF-05 补证](2026-09-22-wf05-supplement.md)。第 1 轮的失败时点/指纹不改写。其 27 项清单与第二轮快照比较，只有 `orchestrator.py` 变化；新增 iCode/test 变化由本轮四项清单单列覆盖。

| 四项文件 | SHA-256 |
|---|---|
| `tom-autodev/scripts/clients/icode_runtime.py` | `a20cd69adbb6153cbc0e472b353a89b11e975141b79fd0c6175d2f2c81a9bf33` |
| `tom-autodev/scripts/orchestrator.py` | `28e5376d2975ac858d77715e9e4944e8d6964afa34a521cd4428582944eb401a` |
| `tom-autodev/scripts/tests/test_icode_profile_guard.py` | `5e383468c2c2a2f6b98de59d514c0007ca40556e85c4d7b348a5e7bd46064e7a` |
| `tom-autodev/scripts/tests/test_runtime_contracts.py` | `25c5b673e95ead69949ef69bb9989f6dea3be7da71bb0bf3e4d84daf2b09988b` |

## 第二轮代码与调用方核对

本节行号针对上述第二轮冻结快照。

| 路径 | 实际检查及副作用顺序 | 正常/恢复边界 |
|---|---|---|
| `Orchestrator.icode_runtime`，`:1367` | 工厂先 `_runtime_profile`；禁止调用者传 `profile_hash`，拒绝覆盖 pinned `submission_policy`，仅将实际 pin/策略装入 runtime。 | `test_runtime_contracts` 改为真实 `_start` 的 profile fixture，仍验证 state/ledger/artifacts/workspace 共用；没有伪造缺 profile 的 run 来让工厂绕过合同。 |
| `Orchestrator._submit_to_ipipe`，`:1470` | 在调用任意注入 runtime 之前检查当前 profile；`:1526` 保留 runtime 返回后的检查，再生成 submission artifact/状态。 | 外层已有的 durable controller receipt 仍先只读返回。正常新提交和实际完成回执回放由 B 补证，不仅用手工注入缓存测试。 |
| `IcodeRuntime._profile_error`，`:68` | 比较构造 pin 与最新批准 pin、磁盘文件 hash，以及构造/当前/文件中的 submission policy。缺文件阻断，任意缓存策略突变不能变更动作。 | 旧直接构造且无 `profile_path` 的 standalone fixture 保留历史语义；正式工厂不接受缺 profile 的受管 run。该兼容边界不能作为磁盘覆盖证据。 |
| `IcodeRuntime.submit`，`:160` | 入口检查在 preflight 之前；preflight 后、remote drift/fetch 后、CR reconciliation 后、同卡 sibling CR 查询后、CLI repository materialize 后再次检查。push 返回后及 `_receipt:596` 写成功回执前检查。 | G7 的 run/gate/hash、persisted review artifact、revision/worktree 和 CR 身份验证未移除。漂移前已合法发出的 push 若确认时漂移，保留未知 intent，不写成功/失败回执冒充确认。 |
| worker/CLI/compatibility | worker 的 SUBMIT 分支仍通过 `_submit` 和 controller；`IcodeClient.submit` 直接转发同一 runtime。没有新增独立 push 实现。 | 第 1 轮高层 `_submit` 的检查仍在，第二轮补足 direct/controller/cached runtime。没有将 workflow-only guard 当作 profile 证明。 |

这组检查没有引入跨磁盘文件、SQLite 和远端平台的原子事务锁；结论限于支持的入口、已识别 I/O 后写入边界和下述实际复现。没有声称防御任意时刻非协作写入 profile 的全局原子隔离。

## A-05 原始反例复跑

直接复用第 1 轮保存的 [原始复现脚本](evidence/2026-09-22-a05-icode-profile-repro.py.txt)，只把输入根目录切到第二轮快照；未改变触发条件或 mock 行为。调用前 `_runtime_profile` 明确为 `PROFILE_CONFLICT`。

| 入口 | 第 1 轮 | 第 2 轮 |
|---|---|---|
| `Orchestrator.submit_to_ipipe` | mock push 1 次，落 OK iCode receipt，最后才返回 PROFILE_CONFLICT | PROFILE_CONFLICT，mock push **0** 次，**无** receipt，run events 不变 |
| 正式工厂构造的 `IcodeRuntime.submit` | mock push 1 次，落 OK receipt，返回 OK | PROFILE_CONFLICT，mock push **0** 次，**无** receipt，run events 不变 |

完整原始输出：[round2-original-repro-output.txt](evidence/2026-09-22-round2-original-repro-output.txt)。原先 P1 影响不再发生；A-05 在本批次已确认的 iCode 扩展触发可以关闭。第 1 轮已验证的 iPipe 修复文件未变化，其证据仍有效。

## 真实正常路径与恢复补证

B 独立执行 [三项补证脚本](evidence/2026-09-22-round2-independent-boundaries.py.txt)，未把新测试写入冻结生产/回归文件。使用第二轮真实 fixture，补上 standalone iCode fixture 所省略的 task 元数据（通过真实 ArtifactStore 新建 reviewed descriptor，未手改数据库），用于完整 controller 路径。

| 场景 | 实际结果 |
|---|---|
| 正常 controller → 实际完成回执 → 漂移后回放 | 真实 `submit_to_ipipe` 完成 `SUBMIT→IPIPE`，push 1 次。改变磁盘 profile 后同动作返回完全相同 durable controller receipt，SQLite dump 和 transport calls 不变。直接低层 runtime.submit 则返回 PROFILE_CONFLICT，无新增调用；它原本需要 preflight/reconciliation，未被声称为纯回执 API。 |
| 未知提交 → 漂移 → 恢复原 pin → 查询确认 | 首次模拟 push timeout 得 SUBMIT_RESULT_UNKNOWN；平台 mock 后来出现准确 CR。漂移期间 runtime 在任何新查询/写入之前阻断；恢复原始 profile 字节、重建工厂后仅核对 CR 并写正确回执，总 push 仍为 1，pending intent 清零。没有盲目重新提交。 |
| 连续两次批准 repin | 策略先改 `one_cr_per_repo`，再改回明确的 `one_cr_per_change_set`，每次使用真实 plan/apply 和准确批准。全部旧实例均在无 DB/transport 变化下 PROFILE_CONFLICT；新工厂采用新策略，最终只 push 1 次并成功。 |

原始 JSON 输出：[round2-independent-output.txt](evidence/2026-09-22-round2-independent-output.txt)。测试脚本和输出的内容 hash 见 [round2-review-evidence.json](evidence/2026-09-22-round2-review-evidence.json)。首次完整 controller 试配因 standalone fixture 缺 `task_id` 元数据得到 REVISION_UNRESOLVED，未计为产品缺陷或通过证据；补齐真实 fixture 合同后才得到上述完整结果。

第二轮 15 项专项还验证：现存漂移时不进入任何注入 runtime；IcodeClient 同样受限；缺 profile 文件；工厂覆盖 policy/hash 拒绝；preflight、CR 查询、materialize 期间漂移；push 后及准确 CR 确认期间漂移保留未知 intent；未知提交漂移后不得继续查询/写入；正常准确 revision 的回执。负例比较 SQLite dump 和 mock 调用，而非只检查错误码。

根任务报告最初 11 项红测含 8 个断言失败与 1 个工厂类型断言错误；后者经 fixture/断言明确化处理，不能作为真实产品失败证明。B 本轮独立原始反例及绿测结果不依赖该错误。

## 验证命令与结果

在第二轮冻结快照的 `tom-autodev/scripts/tests` 执行：

```text
$ PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_icode_profile_guard test_icode_runtime test_runtime_contracts
----------------------------------------------------------------------
Ran 50 tests in 14.744s

OK
```

50 项由专项 15、原 iCode runtime 27、runtime contracts 8 组成；原始逐用例输出为 [round2-review-tests.txt](evidence/2026-09-22-round2-review-tests.txt)。同模块随后分别执行也通过，不重复计作新增场景证据。

根任务最终完整测试的实际保存输出已读取：控制面 [976 tests / 126.720s / OK](evidence/2026-09-22-final-controller-tests.txt)，以及 [tom-review 6 tests / 1.041s / OK、Diagnose shell PASS](evidence/2026-09-22-final-support-tests.txt)。这些输出绑定前述 304 文件清单，B 已核对主仓和本轮快照均匹配。B 未重跑全量套件；`git diff --check` 在主仓 exit 0。

## 本批次矩阵继承与处置

| ID/范围 | 本轮更新 | 结论 |
|---|---|---|
| A-01/B-01；WF-01/07/10 | release advance 拒绝未被本轮触及；第 1 轮证据有效，完整最终回归通过 | 已确认触发关闭 |
| A-02/B-02；WF-02/04 | retry 规范化、run/gate/hash/成员/上限代码未变；第 1 轮证据有效 | 已确认触发关闭 |
| A-03/B-03、A-04/B-04；WF-09 | patchless/source/null 合同代码未变；第 1 轮完整 worker 流程及身份反例证据有效 | 已确认触发关闭 |
| A-05；WF-08 相邻 iCode/iPipe | 第 1 轮 iPipe 修复与本轮 iCode 原始反例、正常/unknown/repin/回放均已验证 | 本批次已确认触发关闭，无新的确认残余缺陷 |
| WF-03/05/06 及未变的其他路径 | 原始初审与第 1 轮影响分析仍有效；WF-05 四项真实 worker 补证不被本轮修改影响 | 继承已列场景，不升级成未测环境通过 |
| GAP-WF08 | publication、background、maintenance 等 profile 入口尚缺完整支配/副作用/回放矩阵 | **INCOMPLETE，待取证，非已确认新 P1** |

## 剩余限制与结束决定

- **C：INCOMPLETE**，绑定 target_commit `daa76f6fee3f8d609943666d3e2b9f456026b2fa`。本批次五项已确认问题的触发与受影响正常路径现有证据支持关闭，但 GAP-WF08 必要证据未齐。后续入口清单、负责人 Tom 和“下一次声明 C PASS 前”结束条件已在 [后续任务](2026-09-22-profile-guard-followup.md) 明确。
- **M：未验证。** 没有准确模型版本、v2 场景 8×3 独立资格执行和原始响应。
- **P：未验证。** 没有真实业务/平台/Comate 部署证据；本地 mock 结果不能代替现场验收。
- **局部复核：通过第二轮 A-05 修复检查，内容已逐 blob 绑定上述 target_commit。** 不代表全量 C PASS；审计报告后续另行提交不改变本报告所评实现身份。
- 没有新增确认 P0/P1/P2，也未做风险接受。本批次两轮预算已用完，复核停止。GAP-WF08 转为明确后续取证任务，不自动第三轮扫描，不继续征集风格或重构建议。
