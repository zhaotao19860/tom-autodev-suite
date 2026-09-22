# 套件评审记录：2026-09-22 首次有界工程验收

**本批次完成两轮修复与定向复核，五类已确认缺陷的触发已关闭。完整 C 结论为 INCOMPLETE，M/P 均未验证。** 唯一尚未关闭的 C 必要证据项是 `GAP-WF08`：不能把 iCode/iPipe 的配置漂移回归推广为所有可写入口都已有等价证明。本批次到此停止，不再开展第三轮开放式评审。

## 固定基线与范围

- 标准：`TAD-REVIEW-EXIT / 1.0`；文件 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。本轮未修改标准或降低门槛。
- 仓库：`https://github.com/zhaotao19860/tom-autodev-suite`；分支：`phase1-workflowspec`。
- 初审 target / 修复 base_commit：`273d2507bcfb600d1a1ab9e5e6d8f61b72d41565`。
- 正式 target_commit：**`daa76f6fee3f8d609943666d3e2b9f456026b2fa`**。后续存放本报告、原始证据和 README 索引的提交仅归档审计资料；本结论不以移动分支名代替实现版本。
- 目标：套件自身 C 工程验收，WF-01…WF-10；controller、schema、skill 合同、worker、维护与适配边界。后续复核只覆盖确认修复及直接调用方，没有增加功能需求。
- 已取证环境：macOS 26.6.2 arm64、Python 3.9.6；临时 Git、SQLite、真实控制器和模拟外部平台。未验证 Linux/跨主机共享文件系统、真实 Comate 部署、真实业务构建或发布。
- 评审者：Codex 主任务及独立上下文 A/B；具体模型版本未获得可复核记录。两个初审在合并结论前独立完成，不把模型意见一致作为事实证明。
- 发起人/维护负责人：Tom；主任务负责确认和修复，剩余风险接受仍由负责人决定。本次没有代为接受风险。
- 预算：最多两轮修复→复核，实际使用两轮；第二轮止于 A-05 的 iCode 相邻入口。没有启动新的同范围审计批次。

规则身份见 [完整清单](evidence/2026-09-22-final-rule-identities.json)：

| 身份 | 固定值 |
|---|---|
| workflow | `workflow-spec-v2`，规范 hash `71f51bcabcd042cf3870e78e57568b0940e0288734c4be4b14a6398aaffb71c6` |
| 16 schemas + 12 SKILL.md | 内容清单 hash `441cdc30089dd033651d4b2b9dda5adc8145bc977ed5b3699d7544910865c30c` |
| 固定行为集 | v2，8 场景，SHA-256 `6c81ea0de6f616e0b20863a0c23b33f61ef0cc640e2faf582220a535993d4d15`；未运行正式 M 资格 |
| profile/prompt | 使用本提交中的合成测试 profile；没有真实项目 profile 或实际模型 producer prompt 身份，不推导 M/P |
| 已测套件内容 | 304 文件 manifest hash `247f229f9daeda8dd8b2eb6ab85cd1a3a17fd5f56dd34f0d852609ffc86217bb` |

清单算法为按相对路径排序，拼接每项 `path + NUL + SHA256(file).digest()`，再求 SHA-256。[提交绑定检查](evidence/2026-09-22-commit-binding.json) 从正式 target 的 Git archive 逐个读取 304 个文件，与 [全量测试冻结清单](evidence/2026-09-22-final-tested-manifest.json) 比对，零差异。

## 评审链与证据继承

1. [独立初审 A](2026-09-22-initial-a.md)、[独立初审 B](2026-09-22-initial-b.md) 均绑定原始 commit。基线已有 911 项控制面回归全绿，但额外复现确认发布、重发审批、两种诊断合同及缓存配置守卫缺陷，原始结论均为 BLOCKED。
2. 第一轮修复后，[定向复核](2026-09-22-targeted-review.md) 运行 74 项相关回归；四类原始问题及 iPipe 触发关闭，同时确认同一 A-05 根因仍影响 iCode。没有把第一轮全量 961 项通过写成 C PASS。
3. [WF-05 补证](2026-09-22-wf05-supplement.md) 用实际 worker 补齐同 run 排他、两 run 共享源仓、租约接管和重放；原始生产代码同样通过，属于补证而非新缺陷。
4. 第二轮只修 iCode factory、controller 提交前检查、缓存 runtime 的策略/pin/I/O/回执边界，并更新一个工厂测试为真实已启动 profile。[最后定向复核](2026-09-22-targeted-review-round2.md) 关闭原 iCode 反例，验证正常提交、真实 controller 回执回放、未知操作恢复和连续 repin；50 项相关回归通过。

第一轮到第二轮的生产变更限于上述 iCode 与 orchestrator 边界；发布、审批、诊断和 iPipe 修复内容保持不变。对应初审与第一轮的正常路径证据经定向复核和最终全量回归继承到本 target；新增 iCode 代码另有 [第二轮冻结清单](evidence/2026-09-22-round2-code-manifest.json)。

初审临时快照有一项已披露差异：`infoflow-gateway/package-lock.json` 根依赖范围与原 commit 不同，来源未知。所有 findings 相关源码均匹配原始 commit；本 target 的该 lockfile 与原 commit 完全相同，未携入此差异。不能把初审临时快照称为全部字节完全一致。

## WF 验收矩阵

以下“通过”均指本地已列举的 C 行为证据；跨项的 `GAP-WF08` 仍阻止整体通过。

| WF ID | 实际证据、结果与边界 | 状态 |
|---|---|---|
| WF-01 | producer/schema/前驱、task/revision、Spec-DAG 验收点以及错误草案恢复回归通过；补测 generic advance 不能用产物名字伪造发布成功。见初审和 `test_producer_validation`、`test_schema_validation`、`test_pipeline_plan`。 | 列举场景通过 |
| WF-02 | `test_approval_gate_retry` 20 项覆盖真实 SQLite、重启草案、G0/G10、watcher→resume、最多三次重发；错 run/gate/hash/成员、REJECT/PENDING、变更内容均不放行。其他 channels/delivery 回归保持通过。 | 列举场景通过 |
| WF-03 | workspace recovery、task5 safety、iCode worktree/preflight 验证准确仓/分支/基线、所有权与中断恢复；WF-05 补证验证用户源仓脏内容保持不变。 | 列举场景通过 |
| WF-04 | phase/state/artifacts 与 runtime 回归覆盖记录、回执、转移断点和重启；新审批恢复可消费；iCode 未知推送不盲重发，漂移后不查询/续写，恢复兼容 pin 后只查询已有 CR。 | 列举场景通过；真实平台最终一致性属于 P |
| WF-05 | 四项实际 worker 补证覆盖同 run 排他、跨 run 共享资源锁及所有权隔离、过期死进程接管和重放；`locks=None` 明确为单进程调用模式，不能证明无锁并发安全。 | 本地 API 合同通过；实际宿主装配未验证 |
| WF-06 | `test_pipeline_plan` 及 iPipe 回归覆盖冻结计划、逐模块依赖、修复后旧 build 失效、共享测试新版本、未变独立模块复用、多模块乱序/重启及旧 run 迁移。 | 列举场景通过 |
| WF-07 | generic advance 一律拒绝新增 RELEASE_SUCCESS；正常发布证据摄取仍校验全部模块、准确 build/revision/environment、G9 与 durable verifier receipt。缺模块、未发布、伪造聚合不能成功。 | 本地平台模拟合同通过 |
| WF-08 | workflow 入口矩阵、批准 repin 及计划漂移回归通过；iPipe 24 项和 iCode 15 项 profile 专项覆盖新写入、I/O 后、旧对象、未知 intent 和只读回放。知识发布/审批后台/维护入口的 profile 等价性未形成完整证据矩阵。 | **INCOMPLETE：GAP-WF08** |
| WF-09 | `test_diagnosis_contract` 五项通过：无 diff 的充分提案经 G6→PLAN；源码 Review 使用 null pipeline 身份；iPipe build/environment/signature/stage/job 必须匹配。repair policy/signature/持久预算回归继续通过。 | 列举场景通过 |
| WF-10 | 假/缺/错回执、制品完整性、跨 prompt/model 缓存、summary 脱敏回归通过；正式实现 SHA 与已测内容零差异；报告明确模拟平台与 M/P 资格边界。 | 列举场景通过，未推导跨模型或生产资格 |

## Findings 与修复处置

完整原始触发、代码定位、影响和反证核对保留于两份初审；本表沿用稳定 ID，不重复创建同根问题。

| 稳定 ID / 级别 | 违反合同与已确认触发 | 修复及定向证据 | 最终处置 |
|---|---|---|---|
| A-01 / B-01，P1 | WF-01/07/10：维护 advance 只凭调用方产物名字写 RELEASE_SUCCESS，无平台发布甚至缺必需模块 | `RELEASE_INGEST_REQUIRED` 关闭旁路；`test_generic_advance_cannot_claim_release_success_without_platform_proofs` 及正常 ingest/全模块发布回归通过 | 已确认触发关闭 |
| A-02 / B-02，P1 | WF-02/04：已批准的 `Gx#retry-N` 被 worker/EvidenceGate/G0/G10 等消费者当成其他 gate，正常恢复卡死 | 消费方仅归一 base gate，保留原 run/hash/decision/成员绑定和重试上限；补齐真实 created_at；20 项专用回归及 restart 正负例通过 | 已确认触发关闭 |
| A-03 / B-03，P1 | WF-09：诊断必须先有 diff，诊断阶段又禁止写补丁，修复方向无法进入 PLAN | JSON/语义校验一致接受 patchless REPAIR；保留原因/计划约束和后续 G5；真实 worker G6 获批重启后进入 PLAN | 已确认触发关闭 |
| A-04 / B-04，P1 | WF-09：源码 Review 失败没有 build/stage/job，却被要求填写非空流水线 ID | schema 允许 null，控制器根据真实前驱区分源码与 iPipe；伪造/错绑身份仍拒绝；五项诊断合同测试通过 | 已确认触发关闭 |
| A-05，P1 | WF-08：缓存 iPipe、iCode direct/controller 在 profile 漂移后仍触发/提交/写回执 | 两轮守卫修复；39 项专用 profile 测试通过；独立重跑原 iCode 两入口反例均 push=0/无 receipt，正常、未知恢复与连续 repin 通过 | 已确认 iPipe/iCode 触发关闭；其他入口证据留在 GAP-WF08 |

缺陷回归曾在未修复代码上产生对应失败：第一轮发布/诊断和重发审批；iPipe 原始触发及 I/O 中漂移；第二轮 iCode 首批 11 项有 8 个预期断言失败、另一个 factory 类型断言原为 AttributeError，后改为明确类型断言。没有用 fixture 错误代替所有缺陷证明。最终新增 65 项回归/补证，完整收集总计 976 项。

## 最终验证

正式 target 的源码与全量执行时一致，提交后由 Git archive 复核；没有因仅新增报告反复运行全部测试。

| 命令或检查 | 实际结果 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q` | 976 tests，126.720s，OK；[原始输出](evidence/2026-09-22-final-controller-tests.txt) |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tom-review/tests -p 'test_*.py' -v` | 6 tests，1.041s，OK；[原始输出](evidence/2026-09-22-final-support-tests.txt) |
| `bash tom-diagnose/tests/test_autodebug.sh` | PASS，退出 0；输出中的 tom-autodebug 为历史测试标签 |
| `git diff --check` / 提交前 cached 差分检查 | 退出 0 |
| 规则与内容身份 | 标准 hash 未变，v2 行为集为 8 场景；304 已测文件全部匹配正式 commit |
| 修改文档链接/锚点 | [检查结果](evidence/2026-09-22-doc-checks.json)，零错误；包括报告与后续任务索引 |

## 剩余风险与后续任务

| ID | 范围与影响 | 处置、负责人及接受决定 | 关闭/重开条件 |
|---|---|---|---|
| GAP-WF08 | publication、审批后台与维护等入口尚无完整 profile 守卫及纯回放等价证明；不代表已确认这些入口全部有缺陷 | 必要待取证，Tom/指定维护者；未接受风险。[明确任务](2026-09-22-profile-guard-followup.md) 已限定入口、证据、修复与停止条件 | 下次声明 C PASS 前补齐；只围绕此范围取证/修复，不再次全仓征集问题 |
| M | 未对确切模型/skill/controller/prompt 运行 v2 固定行为集 8×3 | 未验证，无跨模型效果等价声明 | 选定模型后保存原始响应与机械/语义判分；换模型重新取得 M |
| P | 真实 Comate 锁注入、平台适配与业务交付/失败修复/恢复未在代表性项目验证 | 未验证；没有执行真实平台写操作，没有生产就绪声明 | 在授权项目与环境记录 CR、build、审批、版本、发布核验并由负责人确认 |

## 结论与停止条件

- **C：INCOMPLETE**。已确认的五类缺陷触发修复并通过定向复核，但 GAP-WF08 仍缺必要证据，不能整体 PASS。
- **M：未验证；P：未验证**。976 项控制面测试不代替这些资格。
- **局部修复复核：通过**，范围为上表 A-01…A-05 已确认触发和直接调用方；不提升为全量验收。
- 两轮默认预算已用完，本批次停止。后续仅推进已定义的 GAP-WF08 任务，以及按需获得 M/P；重复表述、模型偏好和额外能力建议不重开本批次。出现新的可验证 P0/P1、真实运行违约、回归失败或合同/环境变化时，按影响另行指定范围。
