# BGW 知识迁入 bgw-workspace 方案

状态：草案（2026-11-02），待负责人确认第 6 节的决定后执行。本方案不改任何仓库。

## 1. 目标与边界

- **目标**：把 BGW 的项目知识（拓扑、流水线、测试规则、仓库关系）从本机的 `tom-project-bgw` skill 和个人记忆，迁到团队共享、经评审维护的 `baidu/sysip/bgw-workspace`；tom-autodev 改为从 workspace 读取这些知识。
- **不变**：tom-autodev 控制面（状态机、审批门、外部写入、证据与发布核验）留在本套件，不进 workspace。审批与证据规则不因迁移改变。
- **不迁**：运行状态（`~/.tom-autodev/*.sqlite`）、凭据（`irepo-tokens.yaml` 等）、单个 run 的执行过程（已在状态库与 KU）、通用语言 skill `tom-lang-c-cpp`。

## 2. 现状

| 知识 | 现在在哪 | 问题 |
|---|---|---|
| 测试拓扑、主机、二进制路径、qa 分支 | `tom-project-bgw/references/runtime-topology.md` | 只在本机；本套件是公开仓库，主机名和人名不该放这里 |
| 日志位置、版本与计数器检查、排障顺序 | 同上，及 `knowledge-and-evidence.md` | 同上 |
| P0新case回归参数映射、发布段各阶段 | 同上 | 同上 |
| 测试分支发布规则（提测跳过、bgwagent 准出要确认） | 个人记忆 `project-bgw-autodev-skip-test-request` | 只有我能看到 |
| P0新case回归不得再跳过 | 个人记忆 `project-bgw-p0-newcase-env` | 同上 |
| 仓库、模块、分支、锁、流水线 ID | `tom-autodev/templates/bgw.template.yaml` | 配置与知识混在一起，没有说明文字 |
| 需求分析与开发文档 | KU 目录（profile 的 `knowledge_sources.ku`） | 保持不动 |

`bgw-workspace` 目前是空模板：`docs/` 只有各分类的 `index.md` 和 `template.md`，`skills/`、`mcps/` 为空。`repos/.repo-list` 列了 7 个仓库，但每行以 ` -ssh://` 开头（空格加横线），不符合文件头说明的“每行一个地址”格式，roma 初始化读取时可能失败，迁移时一并改正。

## 3. 内容映射

| 源 | 目标（bgw-workspace） | 说明 |
|---|---|---|
| runtime-topology「Test Topology」 | `docs/environment/bgw-test-topology.md` | 四条流水线的主机、二进制路径、控制面地址、qa 分支与测试类型的对应 |
| runtime-topology「iPipe Parameter Mapping」「Release path」，记忆 skip-test-request | `docs/environment/ipipe-pipelines.md` | 流水线 ID（348101/348102/348141/348142/504074）、各阶段声明的参数及来源、发布段阶段顺序、测试分支与真实分支的提测/准出差异、准出人 |
| runtime-topology「Who Replaces What」「Associated Logs」「Version/Counter Inspection」，knowledge-and-evidence 的 magic number 条目 | `docs/testing/product-case-triage.md` | 产品用例失败后的排障顺序、每步看哪里、已知误判（`Wrong message magic number: 20210705`） |
| 记忆 p0-newcase-env | `docs/testing/p0-regression-policy.md` | P0新case回归何时可以跳过（仅环境未建好）、建好后必须跑出证据才能进发布 |
| profile 模板的仓库段 | `repos/x86bgw/overview.md`、`repos/bgwagent/overview.md`、`repos/x86bgw-qa/overview.md` | 模块名、用途、目标分支、对应的 ChangePipeline、业务仓与测试仓的关系；不放本机路径 |
| 本次迁移本身 | `artifacts/bgw-knowledge-migration/OUTCOME.md` | 迁了什么、来源、验证方式 |

每篇知识按 workspace 的 Source-Backed Knowledge 规则加 `source` 与 `last_verified`：能从平台实时读到的（流水线配置）写明读取方法，正文只作 fallback。

**留在本套件、不迁的：**
- `tom-project-bgw/SKILL.md` 里的 profile 要求、知识优先级、验证合同，以及 runtime-topology 的「Parent Scheduling Contract」：这些是项目 skill 与控制面之间的接口，不是 BGW 事实。
- 「Reading declarations」一节（读流水线配置而不是 `manualParams`）：适用于所有项目，改放 `tom-autodev/references/phase-protocol.md`。
- 记忆 `feedback-ipipe-params-from-config` 同理，属于通用做法。

`docs/development/`、`docs/architecture/` 暂不写：现有材料里没有经过验证的 BGW 编码规范或架构说明，按 workspace 规定不预建空文档，等真实需求产生后再沉淀。

## 4. 分阶段实施

**阶段 1：只迁内容（不改 tom-autodev）。**
1. 在 bgw-workspace 开一个 CR：写入第 3 节的文档、更新各分类 `index.md`、改正 `.repo-list` 格式。
2. 内容从源文件改写，不整段复制；主机、人名只出现在 workspace（内部仓库）。
3. 由你评审合入。这一阶段 tom-project-bgw 原样保留，两边暂时重复。

**阶段 2：tom-autodev 改为读 workspace。**
1. profile 的 `knowledge_sources` 增加 `provider: workspace`（仓库 `baidu/sysip/bgw-workspace`、本机 checkout 路径）。
2. INTAKE 时把 workspace 的 commit 钉进 G0 的输入（和 profile hash 一样），整个 run 读同一版本；workspace 之后的改动不影响进行中的 run，审批绑定的仍是当时的内容。
3. GRILL/SPEC/PLAN/DIAGNOSE 的 ProducerJob 上下文列出相关 workspace 文档路径；`tom-project-bgw` 的 references 改为指向这些文档。
4. 从本套件删除已迁走的主机、人名和流水线细节，`tom-project-bgw` 只保留接口合同。
5. 回归：profile 校验、G0 哈希包含 workspace commit、workspace checkout 缺失或 commit 不符时返回 `PROJECT_NOT_READY`。

**阶段 3：结论写回（以后再做）。** run 结束后，把已验证的新事实（如新的环境坑）整理成候选，经人工审批后以 CR 写回 workspace 的 `docs/`。审批门与现有门一样按内容哈希绑定。

## 5. 验收

- 阶段 1：bgw-workspace 的 CR 合入；每个分类 `index.md` 指向的文件都存在；正文无 token、密码。
- 阶段 2：下一个真实 BGW run 从钉住的 workspace commit 读取知识并走到 RELEASE_SUCCESS；本套件中不再出现 BGW 主机名与人名。

## 6. 需要你决定

1. **迁移后的权威来源**：建议 workspace 为准，`tom-project-bgw` 只做指引；否则两份会逐渐不一致。
2. **run 读取哪个版本**：建议 INTAKE 时钉住 commit；读分支最新虽然省事，但审批过的内容会在 run 中途变化。
3. **公开套件里的主机名与人名**：建议阶段 2 删除，只留在内部 workspace。
4. **repos/ 写哪些仓库**：建议先写 tom-autodev 实际用到的 3 个（x86bgw、bgwagent、nsiqa/x86bgw）；其余 4 个（newttm、x86bgw-dpdk-17_11、x86bgw-dpdk-2011、acc）等有真实需求时再补。
5. **向 bgw-workspace 提 CR**：这是外部写入，需要你同意后我再开。
