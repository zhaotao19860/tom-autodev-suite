# R11 第 2 轮修复复核 · 分片 B

**结论先行：本片 FAIL，C 状态为 BLOCKED。8 项旧 finding 中，4 项关闭，4 项 P1 部分修复但未真正关闭。未确认本次 diff 引入独立新缺陷；残留沿用原 ID，不重复编号。**

## 基线与范围

- **标准**：已读 `AGENTS.md`；`TAD-REVIEW-EXIT / 1.0`，SHA-256 匹配：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- **仓库/分支**：tom-autodev-suite / mvp。base：`9b8daff8a7693a14a3c150625ed67a688ff2a381`；target：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`。HEAD 匹配，评审前后工作区干净。
- **范围与继承**：按 §5 定向复核上一轮 `2026-11-02-r11-codex-review-1B.md`，检查 iCode/iPipe runtime、restart、state store 及必要审批、CLI、只读查询调用方的相关差分；不重扫全仓。
- **规则身份**：workflow-spec-v2，canonical hash `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；16 份 schema 与 base 无差异，继承 1B 身份。`tom-autodev/SKILL.md` SHA-256：`3d92c2131331cfc9bc987dcad2dae2357947b1602966b83e584560ece19d4f72`。profile 使用 target 提交中的测试夹具；未执行模型 producer/prompt。
- **评审者/日期/负责人/预算**：Devin，具体模型版本未知；2026-11-02；负责人 tom；本批次第 1 次修复→复核，默认预算最多两次。
- **执行边界**：macOS arm64、Python 3.14.4、SQLite 3.53.0。`/tmp` 创建目录实测 `EPERM`，提权被策略拒绝，故改用内联 `python3 -c`、内存 SQLite、fake 平台复现，未保存临时脚本。未修改仓库、未执行业务编译/测试、未调用真实平台。

以下代码路径均相对 **`tom-autodev/scripts/`**。

## 验收矩阵

| 不变量 | 当前证据与结果 | 状态 |
|---|---|---|
| WF-02/03/08：准确授权与目标绑定 | 原 CR 分支漂移已阻断；显式外 project 仍可触发越界推送 | **失败** |
| WF-04/05：未知写入与恢复 | 124/信号退出已保留 UNKNOWN；其他丢应答仍重推；restart 仍漏查旧 patchset build | **失败** |
| WF-06/07/10：构建、发布身份 | 错分支、错误 rebuild 时间归因、多 release 已阻断；外 stage 身份仍能保存成功 proof | **部分失败** |
| WF-08：只读查询不写状态 | CLI `next`、过期 agent lease 下的 `context`、restart `plan` 前后内存数据库快照不变；SUBMIT 查询不执行恢复 | **通过所列场景** |
| 定向回归 | 替换内存/物理事实夹具：iCode 32、iPipe 74、平台 fixture 10、ENV_WAIT 8 项均通过；原样审批纠正模块 2 项通过；`git diff --check 9b8daff..01f123a` 返回 0 | **通过子集，非完整 C** |

## Findings：逐项复核

### 1. R11B-001 / P1 — **未关闭，部分修复**

**位置**：`clients/icode_runtime.py:424–437、294–305`。违反 WF-04/05、MVP §3.1。

- **原场景已修**：真实 `ProcessTransport` 超时返回 124、SIGTERM 返回 −15；两次 submit 现在为 `SUBMIT_RESULT_UNKNOWN → QUERY_REQUIRED`，**只推一次、无拒绝回执、intent 保持 UNKNOWN**。
- **同根因残留**：故障注入远端已接收、列表暂不可见，进程以 **1** 和 `RPC failed; recv failure: Connection reset by peer` 退出。当前两次调用均 `SUBMIT_REJECTED`，实测 **`pushes=2、pending=0、states=[REFUSED,REFUSED]`**。
- **反证与修法**：124/负数守卫不覆盖此路径；拒绝回执允许新 attempt，UNKNOWN fence 无法兜底。应仅凭明确未发送或权威拒绝证据终结；发送后不能判明的非零退出仍须 UNKNOWN、只读核对。

### 2. R11B-002 / P1 — **未关闭，部分修复**

**位置**：`clients/icode_runtime.py:886–896`；写入 `:405–414`，事后模块检查 `:929–933`。违反 WF-02/03/08。

- **原场景已修**：批准追加 main/#41，平台 #41 为 release 分支，当前 **`pushes=0`**。
- **同根因残留**：#41 的 branch、Change-Id 正确，但明确声明 `project=baidu/foreign/repo`。真实 submit 仍放行，fake 平台在当前仓创建 #99；输出 **`SUBMIT_CONFIRMATION_REQUIRED → QUERY_REQUIRED、pushes=1、new_99=true`**。
- **反证与修法**：descriptor、G7 哈希校验通过；`project` 是已有支持字段，事后不采信回执不能撤销已发生的写入。应在 `CR_APPEND_ALLOWED` 前核对已声明的仓库/module/project 身份，冲突先阻断。

### 3. R11B-003 / P1 — **关闭**

**位置**：`clients/ipipe_runtime.py:1941–1946`。

按原两场景对照 base→target：rebuild POST 超时后，候选①无可用时间；②创建早一小时、启动晚于 intent。base 均认领 101 并存回执；target 均 **`QUERY_REQUIRED、receipt_saved=false、posts=1`**。明确创建于 intent 后的正常对照仍确认 101、保存回执。

### 4. R11B-004 / P1 — **关闭**

**位置**：`clients/ipipe_runtime.py:2897–2902`。

原场景中 `branch`、`trigger.branch` 均为 `dev-with-skips`：base discover/monitor 成功；target **`BUILD_QUERY_REQUIRED`，不保存 binding**。正确绑定后平台改为错分支，monitor 返回 **`REVISION_MISMATCH/INVALID`**；main 正常对照成功。

额外混合分支字段线索缺乏明确接口语义依据，不据此新增验收要求或认定缺陷。

### 5. R11B-005 / P1 — **关闭**

**位置**：`clients/ipipe_runtime.py:2377–2406`。

用原场景调用真实 `verify_planned_release`：同 revision 的 branch-A、branch-B 分别含 release-A、release-B。当前查询两个 release stage，返回 **`RELEASE_AMBIGUOUS、candidates=[release-A,release-B]`**，**状态写入增量 0、无 proof**。单一正常 release 仍可验证。

### 6. R11B-006 / P1 — **未关闭，部分修复**

**位置**：`clients/ipipe_runtime.py:2384–2395、2918–2927`；proof 持久化 `:2467–2477`。违反 WF-07/10。

- **原组合场景已修**：外 module/pipeline build、外 owner-build/pipeline detail、外 module release job 均拒绝，不存 proof。
- **同根因残留**：请求 `stage_detail("rel-stage")`，响应仅将 `entities.stageBuild.stageBuildId` 改为 `foreign-stage`，其余身份正确；或仅将 job wrapper 的 `stageBuildId` 改为外 stage。两种真实 `verify_planned_release` 调用均得到 **`OK、release_id=foreign-release、proof_saved=true`**，各新增一条 proof。
- **反证与修法**：新守卫未接收 expected stage ID，也不检查 wrapper 的 stage 归属；冻结计划及下游仅核对由 target 填充的 proof，不能恢复源响应身份。应逐层比较已声明的 stage/build/pipeline 身份，显式冲突即拒绝生成 proof。

### 7. R11B-007 / P1 — **未关闭，部分修复**

**位置**：`restart.py:64–78、244–262`；`orchestrator.py:4904–4915`。违反 WF-04/05、MVP §3.4。

- **原单 revision 场景已修**：未绑定自动 build 为 RUNNING 或查询 403，均阻断；SUCC 正常对照允许重开。
- **同根因残留**：同 CR #41 有 old/new 两份确认提交回执，均未绑定 build；平台 old build 仍 RUNNING，new build 已 SUCCESS。`_submitted_crs` 按 CR 覆盖旧 revision，生产观察器只查询 new revision。实测 **`RESTART_READY → RESTARTED、target_created=true、old_running_queried=false`**。
- **反证与修法**：本地 binding 为空，intent/event CAS 不会发现漏查的自动对象；`accept_merged` 不能替代 build 处置。应保留源 run 所有已确认提交 revision，发现关联 build 后与本地绑定取并集，再逐个验证终态。

### 8. R11B-008 / P2 — **关闭**

**位置**：`state_store.py:518–525`；`restart.py:220–223`。

真实执行 A→B→C 后 `ANALYZE`：旧 OR 查询计划为 **`SCAN run_lineage`**，返回入边 A→B；target 精确出边查询返回 B→C。`plan(B)`、`execute(B)` 均为 **`RESTART_REPLAY、target=C`**，前后数据库快照不变，不再依赖 SQLite 返回顺序。

## 剩余风险与处置

| 项目 | 处置、负责人及重开条件 |
|---|---|
| R11B-001、002、006、007 | **确认 P1，未关闭**；负责人 tom。不能以风险接受代替修复。下一步仅修复上述残留及对应正常/失败回归。 |
| 执行覆盖限制 | 内存夹具替换了存储、部分 Git/所有权/产物物理事实，不证明落盘、文件锁及跨进程恢复。原样平台 fixture 有 3 项因 tempfile 不可写中断，改内存后通过；完整 C 汇总须保留该缺口，不能引用 CHANGELOG 的全绿声明代替实测。 |

## 结论

**C：BLOCKED，仅就 B 片；M、P：未验证。** 本轮不确认独立新增 `R11-` finding，同根因残留全部沿用旧 ID。按 §5 停止本轮扫描，后续只做有界修复与复核；不代表 A/C 片或全量工程验收结论。

**本片结论：FAIL。**