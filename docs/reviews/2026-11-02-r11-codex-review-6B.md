**结论：FAIL。R11B-001（P1）未关闭。** 完整输出下的多目标部分成功已修复；但接受目标的输出缺失时，仍会误写拒绝回执并允许重推。属于原项残留，**无新增独立 `R11B6-` finding**。

# 套件评审记录 R11B6

- **标准**：TAD-REVIEW-EXIT / 1.0；已读取 AGENTS.md，标准 SHA-256 核对一致：  
  `5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- **仓库/分支**：tom-autodev-suite / mvp；HEAD 等于 target，工作区干净。
- **base_commit**：`52128a7ed2f498863e29733c4eaa24a2324d0852`
- **target_commit**：`ed34c1cbd76f7816cacae454de21dc0b2b6377ab`
- **范围/环境**：C 局部复核，仅 R11B-001、修复差分及提交恢复调用链；Darwin arm64、Python 3.14.4、SQLite 3.53.0。
- **规则身份**：workflow=`workflow-spec-v2`，hash=`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；`tom-autodev/SKILL.md` SHA-256=`3d92c2131331cfc9bc987dcad2dae2357947b1602966b83e584560ece19d4f72`。业务 profile、schema/prompt 资格不适用。
- **评审者/日期**：Devin，精确模型版本未知；2026-11-02。负责人：tom；用户指定第 6 轮，本次一次，不自动续轮。
- **继承证据**：`docs/reviews/2026-11-02-r11-codex-review-5B.md` 的多 push URL 场景与可达性分析；本轮重新验证控制流。

## 验收矩阵

执行 `python3 -B -c` 内联复现：使用内存 SQLite，保留真实 `submit`、审批、intent/receipt 逻辑；注入 transport、本地 Git/工作区观察和内存 artifact。不运行磁盘夹具。

| 不变量/场景 | 当前证据与实际结果 | 状态 |
|---|---|---|
| WF-04/05：完整多目标部分成功 | A 接受、B 拒绝：`SUBMIT_RESULT_UNKNOWN → QUERY_REQUIRED`；push=1、receipts=0、pending=1。base 同输入会推两次 | 通过 |
| 接受证据仍存在的变体 | 两个 `To` 但接受行缺失；缺 `To` 但接受行仍在；多 ref 的空格/`+`/`*` 状态、new branch/tag、Gerrit SUCCESS；stdout/stderr 双向分流，均保持 UNKNOWN，CR 可见后只读收敛 | 通过 |
| **接受证据全部缺失** | A 整段输出丢失、无 `To` 仅剩拒绝、多 ref 接受行缺失：均 `REJECTED → REJECTED`，push=2、receipts=2、pending=0。15 个部分成功注入场景中，12 个安全、3 个残留 | **失败** |
| 真实单目标拒绝及重试 | Gerrit 两种拒绝、Git rejected、missing Change-Id、远端 URL 仓名提取失败，共 5 种：首次结束 attempt，修正后成功；状态为 `[REFUSED, CONFIRMED]`，合计两次 dispatch | 通过 |
| 本地缺 Change-Id | claim 前返回 `CHANGE_ID_MISSING`，零 intent、零 push；修正本地事实后成功 | 通过 |
| 继承未知结果场景 | 权限文案、RPC reset、Empty reply、超时、信号、空输出等 12 种均 UNKNOWN→只读核对，只推一次 | 通过 |
| 差分回归 | 检查全部 6 个差分文件；未确认新增独立功能缺陷。两份 Python AST、代码差分检查通过；全差分 `--check` 退出 2，仅因新增上轮 5B 报告第 7 行 Markdown 硬换行空格，不列功能缺陷 | 功能检查通过 |

## Findings

| ID / 严重级别 | 违反合同 | 触发、预期/实际、影响 | 当前证据及反证核对 | 处置 |
|---|---|---|---|---|
| **R11B-001 / P1** | WF-04/05；`docs/MVP_CONVERGENCE_PLAN.md:90–96`：未知结果只能核对，不能直接重发 | A 已接受，但 A 的目标行及接受行全部缺失；应保留 UNKNOWN，实际认定整次拒绝、释放目标并再次 dispatch | `tom-autodev/scripts/clients/icode_runtime.py:1011` 将“观察到 ≤1 个目标且没有接受标记”当成未落地证明；`:431–438` 写拒绝 receipt，`:296–305` 建后继 intent。审批及工作区守卫未证明实际只有一个目标；新 attempt 查询仅在 CR 已可见时防重 | **确认未关闭，部分修复；沿用旧 ID** |

**最小失败证据**：A 已接受但其输出缺失，注入 `returncode=1、stdout=""`，stderr 仅为：

```text
To https://icode-b.example/repo
 ! [remote rejected] HEAD -> refs/for/main (prohibited)
```

真实 `submit` 路径结果：

```text
首次 → 重放：SUBMIT_REJECTED → SUBMIT_REJECTED
push=2，receipts=2，pending=0，states=[REFUSED, REFUSED]
CR 可见后：OK，push 仍为 2
```

删除上述 `To` 行，结果相同。另测同目标的第二个 Change Set：完整输出时被 `TARGET_INTENT_UNRESOLVED` 阻断；截断输出误拒绝后可再次 dispatch。`retry_allowed=False` 没有阻止后继路径。

**建议修法**：拒绝结论必须依据 dispatch 时绑定的实际目标/refs及完整结果，证明整次未落地；无法证明则 UNKNOWN。保留可证明未发送的本地错误路径。不能仅将 `<=1` 改为 `==1`，因为只剩 B 的目标行仍会误判。补充上述缺失输出及列表延迟回归。

## 剩余风险与改进

| ID/限制 | 范围与影响 | 处置/理由 | 负责人及接受证据 | 到期/重开条件 |
|---|---|---|---|---|
| R11B-001 | 未知推送被错误终结并重发 | P1 阻断，转有界修复 | tom；无接受记录 | 完成整次未落地证明及对应回归 |
| 取证边界 | 截断及多 ref 为用户指定故障注入 | 当前 ProcessTransport 正常返回时完整捕获输出；未证明现用 CLI 实际丢失 A 输出，也不声称平台已创建重复 CR | 不作为平台资格证据 | 授权扩大平台验证时另行取证 |

## 结论

- **C：BLOCKED，仅针对本片 WF-04/05；不代表全量 C 重评。**
- **M：未验证；P：未验证。**
- **局部复核：FAIL，R11B-001 未关闭。**
- 未修改仓库、未联网、未运行磁盘 unittest、业务编译/测试或真实平台操作。按第 5 节停止本次复核，下一步为上述有界修复，不自动追加轮次。

**最终结论：FAIL。**