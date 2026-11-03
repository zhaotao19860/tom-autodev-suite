# tom-autodev 接入 bgw-workspace 设计

状态：阶段 1、阶段 2 已完成（2026-11-02；阶段 1 为 bgw-workspace CR 123095131）；阶段 3 实施中。依据 [BGW_WORKSPACE_MIGRATION_PLAN.md](BGW_WORKSPACE_MIGRATION_PLAN.md) 第 6 节，负责人按全部建议执行：workspace 为准、INTAKE 钉 commit、公开套件删主机人名、repos 先写 3 个仓库、阶段 1 的 CR 由负责人评审合入。

分工：本文档由 Claude 写；代码与 workspace 文档由 Codex 按本文档实现；Claude 评审、跑全量测试后合入。每个阶段一个提交。

## 阶段 1：BGW 知识写入 bgw-workspace

**仓库**：`baidu/sysip/bgw-workspace`，本机 checkout `~/.tom-autodev/worktrees/workspace/bgw-workspace`，分支 `master`，新建分支 `bgw-knowledge-v1`。

**写入文件**（中文；每篇带 `title`/`source`/`last_verified: 2026-11-02` 头，正文固定四节 `Content Summary`/`Key Information`/`Reading Guidance`/`Retrieval`，见 workspace `docs/README.md`）：

| 文件 | 来源（suite 内） | 内容 |
|---|---|---|
| `docs/environment/bgw-test-topology.md` | `tom-project-bgw/references/runtime-topology.md` 的 Test Topology、Who Replaces What | 四条流水线的主机、二进制路径、控制面地址、qa 分支与测试类型；P0新case回归只换 client 侧产物 |
| `docs/environment/ipipe-pipelines.md` | 同文件 iPipe Parameter Mapping、Release path；记忆「BGW 测试分支提测跳过」 | 流水线 ID、各阶段声明参数与产物来源、发布段阶段顺序、测试分支与真实分支的提测/准出差异及准出人 |
| `docs/testing/product-case-triage.md` | 同文件 Associated Logs、Version/Counter Inspection；`knowledge-and-evidence.md` | 排障顺序与每步位置、已知误判 magic number |
| `docs/testing/p0-regression-policy.md` | 记忆「P0新case回归跳过仅限环境未建好」 | 何时可跳过、建好后必须跑出证据才能发布 |
| `repos/x86bgw/overview.md`、`repos/bgwagent/overview.md`、`repos/x86bgw-qa/overview.md` | `tom-autodev/templates/bgw.template.yaml` | 模块名、用途、目标分支、ChangePipeline/BranchPipeline ID、业务仓与测试仓关系；不写本机路径 |
| `artifacts/bgw-knowledge-migration/OUTCOME.md` | — | 迁了什么、来源、验证方式 |

同时：更新 `docs/environment/index.md`、`docs/testing/index.md`、`repos/README.md` 的索引条目；把 `repos/.repo-list` 每行开头的 ` -` 去掉。不写 token、密码；主机名与人名只出现在此内部仓库。

**交付**：本地 commit，推到 `refs/for/master` 生成 CR（经负责人同意的外部写入），负责人评审合入。

## 阶段 2：tom-autodev 从 workspace 读取知识

### 2.1 profile

`knowledge_sources` 增加 `provider: workspace`（schema 的 enum 增加该值）：

```yaml
- provider: workspace
  priority: 0
  repository: baidu/sysip/bgw-workspace
  path: /Users/tom/.tom-autodev/worktrees/workspace/bgw-workspace
  revision: master
  search_scope: BGW team knowledge (environment, testing, repos)
```

`project_registry` 校验：至多一个 workspace 源；`path` 必须是 git 仓库；`repository`、`path` 必填。模板 `bgw.template.yaml` 增加此源（`path: __WORKSPACE_KNOWLEDGE_PATH__`），`profile_wizard` 可让用户填写。

### 2.2 INTAKE 钉住 commit

`Orchestrator._intake_event`：profile 有 workspace 源时读 `git -C <path> rev-parse HEAD`，写入 payload `knowledge_workspace = {"repository", "path", "commit"}`。

- `intake_hash_version` 增加 `v6`，在 v5 字段集上加 `knowledge_workspace`；有该字段才用 v6，否则保持原版本（不改变既有 run 的 G0）。
- 读不到 commit（路径缺失、非 git）返回 `PROJECT_NOT_READY`，`missing: ["knowledge_sources.workspace"]`。

### 2.3 producer 读取同一版本

新模块 `workspace_knowledge.py`：

- `pinned(events) -> dict | None`：取 INTAKE 的 `knowledge_workspace`。
- `verify(pin) -> str | None`：`git cat-file -e <commit>^{commit}` 在该 path 可用，否则 `WORKSPACE_COMMIT_UNAVAILABLE`。
- `documents(pin, phase) -> list[dict]`：按阶段返回要读的文件（`git ls-tree -r --name-only <commit>`，按下表前缀过滤，只列存在的），每项 `{"path", "commit", "show": "git -C <path> show <commit>:<file>"}`。

| 阶段 | 读取前缀 |
|---|---|
| GRILL、SPEC、TASKS | `docs/`、`repos/` |
| PLAN、IMPLEMENT、REVIEW | `docs/development/`、`docs/testing/`、`repos/` |
| DIAGNOSE | `docs/environment/`、`docs/testing/`、`repos/` |

`phase_protocol` 构造 producer action 时，若 run 有 pin，加字段 `knowledge_workspace: {"commit", "documents": [...]}`（不进 `input_hash`，展示用）；`verify` 失败时 action 返回 `WORKSPACE_COMMIT_UNAVAILABLE`，不产出 job。`agent_bridge.context` 原样透出该字段。子 skill（tom-grill/spec/plan/implement/diagnose）的 SKILL.md 各加一句：action 带 `knowledge_workspace` 时先按 `show` 读这些文件，它们优先于项目 skill 里的同名知识。

### 2.4 本地去重（在阶段 1 CR 合入之后）

- `tom-project-bgw/references/runtime-topology.md`、`knowledge-and-evidence.md` 删除；`SKILL.md` 只留接口合同（profile 要求、知识优先级、验证合同、Parent Scheduling Contract），并写明 BGW 知识见 workspace 的哪些路径。
- 「Reading declarations」一节移到 `tom-autodev/references/phase-protocol.md`。
- 本机记忆 `project-bgw-p0-newcase-env`、`project-bgw-autodev-skip-test-request` 删除，`MEMORY.md` 改为一条指向 workspace 的记忆。
- 全仓扫描：公开套件内不再出现 `bjkjy-` 主机名与 `yueyufei`、`liuman`、`houpingping`、`zhangwenxing` 等人名。平台录制夹具里的 job 名（如「25G 环境搭建-sep5」）是流水线配置原文，不是主机地址，保留；`test_platform_fixtures` 的标记检查覆盖上述人名。

### 2.5 回归

- profile：workspace 源校验通过/缺 path/非 git 被拒。
- INTAKE：有源时 payload 含 commit、`intake_hash_version=v6`、G0 哈希覆盖 commit；无源时版本不变。
- action：带 `knowledge_workspace.documents`，按阶段过滤；commit 不可用时 `WORKSPACE_COMMIT_UNAVAILABLE`；workspace 之后有新提交不影响已钉住的 run。

## 阶段 3：验证过的结论写回 workspace

### 3.1 候选

`DIAGNOSE` 草案 schema 增加可选字段 `knowledge_candidates: [{"target": "docs/environment/<file>.md", "title", "body", "reason"}]`（`target` 必须在 `docs/` 下、以 `.md` 结尾，不得含 `..`）。tom-diagnose SKILL 说明：只有环境/流程类、可复用的结论才写候选。

### 3.2 何时写回

run 到 `RELEASE_SUCCESS` 后，worker 汇总该 run 所有已批准 G6 诊断里的候选（去重、过滤掉 workspace pin 中已存在且内容相同的），生成一个写回提案，开审批动作 **`KNOWLEDGE_WRITEBACK`**（不是 G 门：不进 `workflow_spec` 的门表与策略哈希，已钉住的 run 不漂移；做法同已删除的 `PROFILE_REPIN` 动作）：

- 输入哈希 = 提案（`workspace commit` + 各候选的 `target`/内容 sha256）的规范哈希。
- 审批卡显示每个候选的目标文件与正文；按钮只有同意/停止（不可需修改）；停止只放弃写回，不改 run 的终态。
- 没有候选时不开审批。

### 3.3 写入

`KNOWLEDGE_WRITEBACK` 批准后，控制器在 workspace 仓库新建分支 `tom-autodev/<run_id 前 12 位>`，基于钉住的 commit，写入候选（新文件，或在已有文件末尾追加 `## <title>` 一节），更新所在分类 `index.md`，commit（Change-Id 由 commit-msg hook 生成），用 iCode CLI `push_cr` 推到 `master`。走现有 icode 外部写入的 intent/receipt 与未知结果处理（与业务 CR 同一套），CR 由人评审合入。

### 3.4 回归

候选 schema 校验；无候选不开审批；审批哈希覆盖每个候选；未批准不写；推送未知结果只读核对；路径越界被拒。

## 不在本次范围

- Lumos 云端运行 workspace。
- 其余 4 个仓库的 `repos/` 说明。
- `docs/development/`、`docs/architecture/` 内容（无经验证材料）。
