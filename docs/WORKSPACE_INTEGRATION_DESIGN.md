# tom-autodev 接入 bgw-workspace 设计

状态：三个阶段均已实现（2026-11-02；阶段 1 为 bgw-workspace CR 123095131）。依据 [BGW_WORKSPACE_MIGRATION_PLAN.md](BGW_WORKSPACE_MIGRATION_PLAN.md) 第 6 节，负责人按全部建议执行：workspace 为准、INTAKE 钉 commit、公开套件删主机人名、repos 先写 3 个仓库、阶段 1 的 CR 由负责人评审合入。

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

`diagnosis.schema.json` 增加可选数组 `knowledge_candidates`，每项为 `{target, title, body, reason}`，四个字段均为非空字符串。`schema_validator` 在 diagnosis 语义检查中校验目标路径：

- 必须匹配 `^docs/[a-z0-9-]+(/[a-z0-9._-]+)*\.md$`，不得含 `..`。
- 文件名不得为 `index.md`、`template.md`、`README.md`；索引由写回脚本维护，不作为候选目标。
- 越界路径、空字段等不合法候选拒绝摄取，不到写入时才处理。

`tom-diagnose/SKILL.md` 与 `references/phase-contract.md` 约定：只有**环境/流程类、有证据、跨 run 可复用**的结论才写候选，业务代码结论不写；`reason` 说明结论依据与复用理由。

### 3.2 只读提案与申请审批

模块 `tom-autodev/scripts/knowledge_writeback.py` 提供 `plan` → `request` → `execute` 三段入口；写回由**显式 CLI** 发起，不是 worker 的终态自动动作。以下命令从套件根目录执行：

```bash
# plan：只读，不开审批、不写 workspace
python3 tom-autodev/scripts/orchestrator.py knowledge-writeback RUN_ID
# request：只投递审批，不执行写回
python3 tom-autodev/scripts/orchestrator.py knowledge-writeback RUN_ID --request
# execute：只有同意该提案后才执行；A/H 为本次审批 ID 与输入哈希
python3 tom-autodev/scripts/orchestrator.py knowledge-writeback RUN_ID --approval-id A --input-hash H
```

`plan(orchestrator, run_id)`：

1. run 必须为 `RELEASE_SUCCESS`，否则返回 `WRITEBACK_RUN_NOT_RELEASED`；INTAKE 必须有 `knowledge_workspace` pin，否则返回 `WRITEBACK_NO_WORKSPACE`。不补 pin，不使用当前 HEAD 替代 pin commit。
2. 汇总该 run **所有** G6 已 `APPROVE` 且审批 `input_hash` 与诊断产物绑定一致的产物中的 `knowledge_candidates`；排除未批准或绑定不符的产物。
3. 按 `(target, body)` 去重；在 pin commit 上用 `git show <commit>:<target>` 检查，剔除目标文件中已包含完全相同 `body` 的候选，不以标题相同或语义近似代替。
4. 没有剩余候选返回 `{ok: true, reason_code: "WRITEBACK_NOTHING"}`，request 也不开审批。有候选返回 `{ok: true, reason_code: "WRITEBACK_READY", proposal, input_hash}`，其中 proposal 结构为：

```json
{
  "run_id": "...",
  "repository": "...",
  "commit": "...",
  "candidates": [
    {
      "target": "docs/environment/example.md",
      "title": "...",
      "body": "...",
      "reason": "...",
      "body_sha256": "...",
      "source_artifact_id": "..."
    }
  ]
}
```

`body_sha256` 是正文哈希，`input_hash` 是**整个 proposal 的规范 JSON SHA-256**，包括 run、仓库、pin commit 及各候选全部字段；任何候选内容或来源变化都不能复用旧批准。

`ACTION = "KNOWLEDGE_WRITEBACK"` 是审批动作，**不是 G 门**：不进入 `workflow_spec` 门表与策略哈希，不加入 `approval_ledger._REVISABLE_GATES`，不因阶段 3 改变旧 run 的 workflow pin。`request(orchestrator, run_id, ...)` 使用 plan 的输入哈希、profile 成员及现有 `request_infoflow_approval` 投递；`approval_summary` 的 pinned context 展示每个候选的目标文件、标题与正文。审批卡只有**同意 / 停止**，不支持“需修改”。中文合同：

- 审批对象：把本次运行验证过的结论写回 workspace。
- 批准后：在 workspace 提一个 CR，合入仍由人评审。
- 停止：只放弃写回；`REJECT` 不改变 run 的 `RELEASE_SUCCESS` 终态。

### 3.3 批准后写入与恢复

`execute(orchestrator, run_id, approval_id, input_hash)` 重新 plan；提案哈希必须一致，审批必须为 `APPROVE`、动作为 `KNOWLEDGE_WRITEBACK`、run 与输入哈希一致，否则拒绝，不能写文件或推送。

在 pin 的 workspace 仓库 `path` 上：

1. 以 pin commit 为基，使用 `git worktree add` 建立临时 worktree 与分支 `tom-autodev/<run_id 前 12 位>`。不切换或改写用户当前 checkout，保留其分支与工作区内容，结束时清理临时 worktree。
2. 对每个候选：目标不存在则新建，写 `# <title>` 加正文；已存在则在末尾追加 `## <title>` 一节。新文件在所属分类目录 `index.md` 的 Knowledge Map 中追加一条链接，已有文件追加小节不重复加链接。
3. workspace 根 `CHANGELOG.md` 若存在，在顶部追加当天条目；不要求原仓库必须有该文件。提交首行如下，正文列出各候选，末尾保留确定性 Change-Id：

```text
<卡片ID> docs: tom-autodev 运行 <run_id 前 12 位> 写回知识

<逐项列出候选>

Change-Id: I<40 位 hex>
```

Change-Id 用内容哈希生成以保证幂等，**不依赖 commit-msg hook**。外部写入用 `state.claim_intent(run_id, "workspace.writeback", "workspace.writeback:<run_id>:<input_hash>", payload)` 记录，已确认 receipt 可幂等重放。

通过 iCode CLI `push_cr --repo-path <临时 worktree> --branch master` 提 CR；不使用真实平台作为本地测试依赖，也不直接合入。 为兼容现有 CLI 的 `.git` 目录检查，推送时仅将临时 worktree 的 gitfile 替换为指向其专属 gitdir 的目录链接；核对 HEAD、父 commit、common gitdir 与全部 push URLs 不变，结束后恢复 gitfile 再清理，不改用户仓库配置。成功从输出解析 CR URL 并写 receipt，返回 `WRITEBACK_PUSHED`；已有确认回执返回 `WRITEBACK_REPLAY`。

推送判定复用 `clients/icode_runtime.py` 的规则：非零退出默认 `UNKNOWN`，仅当 `_push_refused(output, _single_push_target(repo))` 为真时才能认定拒绝、结束本次尝试。超时、失联或多推送地址下无法证明整体拒绝，均不能当未落地重推；单推送地址的明确 Gerrit 拒绝则结束该尝试。

`UNKNOWN` 后再次 execute **只读核对，不重新推送**：按同一确定性 Change-Id 用 `icode api get_repo_reviews --repo <repository> --status NEW -o json` 查找，确认后补回执；查询未确认则保持未决，不能因列表为空便重推。

CLI 的 `WRITEBACK_READY`、`WRITEBACK_NOTHING`、`WRITEBACK_PUSHED`、`WRITEBACK_REPLAY` 为成功退出码；拒绝或未知结果不冒充成功。审批停止仅放弃写回，不触发 run 状态迁移。

### 3.4 回归

测试入口：`tom-autodev/scripts/tests/test_knowledge_writeback.py`。使用临时 Git 仓库作 workspace，注入假 transport 代替 iCode CLI，不调用真实平台、不运行业务项目编译/测试。覆盖：

- 候选 schema/语义校验：越界路径、`index.md`、其他保留文件名与空字段被拒。
- 非 `RELEASE_SUCCESS` 或无 workspace pin 被拒；无候选不开审批；pin 中已有完全相同正文被剔除。
- 提案哈希覆盖每个候选，改一个字节哈希即变；未批准、审批动作/run 不符或哈希不一致均不写。
- 批准后新文件与已有文件追加两种写法；新文件 Knowledge Map 与已有根 CHANGELOG 更新；提交带卡片号与确定性 Change-Id。
- 用户原 checkout 的分支与工作区不变；临时 worktree 清理。
- 推送超时 UNKNOWN 后重跑只查询、不重推；单推送地址的明确 Gerrit 拒绝结束尝试；`REJECT` 不改 run 终态。

集成实现完成后，在 `tom-autodev/scripts/tests` 下运行 `python3 -m unittest discover -q` 记录实际全量结果；文档状态不替代测试或真实平台验收证据。

## 不在本次范围

- Lumos 云端运行 workspace。
- 其余 4 个仓库的 `repos/` 说明。
- `docs/development/`、`docs/architecture/` 内容（无经验证材料）。
