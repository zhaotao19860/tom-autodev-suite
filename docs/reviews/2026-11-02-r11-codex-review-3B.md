**结论先行：本片 FAIL。** 上轮未关闭的 4 项 P1 中，**3 项关闭，R11B-001 仍未关闭**。未确认相关 diff 引入独立新增 `R11-` finding；残留沿用原 ID。**C：BLOCKED，仅就 B 片；M、P：未验证。**

## 基线与范围

- **标准与代码**：已读 `AGENTS.md`、`TAD-REVIEW-EXIT / 1.0`；标准 SHA-256 匹配 `5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。仓库/分支：tom-autodev-suite / mvp；base_commit：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`；target_commit：`e41578cdddc09f013844c83acd2aceccd3930559`。HEAD 匹配，前后工作区干净。
- **范围与身份**：按 §5 定向复核 `docs/reviews/2026-11-02-r11-codex-review-2B.md:1`，检查 iCode/iPipe runtime、restart 相关差分及必要调用方。workflow-spec-v2，canonical hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；16 份 schema、skill 与 base 未变，继承 2B 身份；profile 使用 target 中测试夹具，未运行模型 producer/prompt。
- **评审记录**：Codex，具体模型版本未知；2026-11-02；负责人 tom；本批第 3 轮、第二次且最后一次修复复核。环境：macOS arm64、Python 3.14.4、SQLite 3.53.0。

## 验收矩阵

| 不变量 | 本轮证据与实际结果 | 状态 |
|---|---|---|
| WF-02/03/08：准确授权、目标绑定 | 外 project/错分支追加均在推送前阻断；正确目标正常追加 | 通过所列场景 |
| WF-04/05：未知写入、恢复 | 丢应答仍存在重复推送；旧 patchset 的运行中/不可读 build 已阻断 restart | **部分失败** |
| WF-06/07/10：构建与发布证据身份 | 外 stage 不生成 proof，正常 release 生成 proof；相关定向回归通过 | 通过所列场景 |

## Findings：逐项复核

- **R11B-001 / P1 — 未关闭，同根因残留。**  
  位置：`tom-autodev/scripts/clients/icode_runtime.py:424`、`tom-autodev/scripts/clients/icode_runtime.py:431`；违反 WF-04/05、MVP §3.1。上轮原文案 `RPC failed; recv failure: Connection reset by peer` 已修：两次 submit 为 `SUBMIT_RESULT_UNKNOWN → QUERY_REQUIRED`，仅推一次、intent 保持 UNKNOWN。  
  **仍失败的场景**：fake 平台已接收但列表暂不可见，命令返回 `128`，stderr 为 `fatal: unable to access 'https://icode.example/repo': Empty reply from server`。两次真实 `submit` 调用均返回 `SUBMIT_REJECTED`，实测 **`pushes=2、pending=0、states=[REFUSED,REFUSED]`**；预期应保留 UNKNOWN、只读核对。  
  **反证与修法**：新增正则未覆盖该丢应答；拒绝回执又被 `tom-autodev/scripts/state_store.py:1919` 允许创建后继 attempt，UNKNOWN fence 因而不能兜底。应仅凭已证明未发送或权威拒绝证据终结；其余无法确定结果的非零退出默认 UNKNOWN，而非继续枚举错误文案。

- **R11B-002 / P1 — 关闭。**  
  位置：`tom-autodev/scripts/clients/icode_runtime.py:896`。复现批准追加 main/#41、目标声明 `project=baidu/foreign/repo`：返回 **`CR_IDENTITY_AMBIGUOUS、pushes=0`**；错分支原场景同样阻断。正确 project/main 对照返回 **`OK、pushes=1`**。真实 descriptor 与 G7 校验保留，未以绕过审批替代验证。

- **R11B-006 / P1 — 关闭。**  
  位置：`tom-autodev/scripts/clients/ipipe_runtime.py:2385`、`tom-autodev/scripts/clients/ipipe_runtime.py:2922`。分别只把详情 `stageBuild.stageBuildId`、job wrapper 的 `stageBuildId` 改成外 stage，调用真实 `verify_planned_release`，均为 **`RELEASE_WAITING、proof_saved=false、数据库写入增量=0`**。正确身份对照返回 OK，并保存一条 proof；录制平台 release 夹具回归通过。

- **R11B-007 / P1 — 关闭。**  
  位置：`tom-autodev/scripts/restart.py:76`、`tom-autodev/scripts/restart.py:258`。同 CR #41 保存 old/new 两份确认回执，均无本地 build binding；使用生产 `_restart_cr_builds` 观察器、fake API。old build RUNNING、new build SUCCESS 时，确认查询两个 revision，**plan/execute 均为 `RESTART_SOURCE_UNRESOLVED`，无目标 run、写入增量 0**。旧 revision 查询 403 亦阻断；两份 build 均 SUCCESS 时，正常 `RESTART_READY → RESTARTED`。

## 验证与剩余风险

- **差分与回归**：相关 diff 未确认独立新增缺陷。定向 unittest：iCode 8、iPipe 10、restart 5、录制平台夹具 10，共 **33/33 通过**；`git diff --check 01f123a..e41578c` 返回 0。R11B-003/004/005/008 继承 2B 的关闭证据；003 相关时间守卫未变，004/005/008 对应回归本轮再次通过。
- **执行限制与处置**：临时目录探测失败，包含 `/tmp`，无法落盘脚本；改用内联 `python3 -B -q`、内存 SQLite、fake 平台及替换的本地物理事实夹具。因此不证明落盘、文件锁或跨进程恢复；未修改仓库、未执行业务编译/测试、未调用真实平台。**R11B-001 仍是确认 P1，负责人 tom，不能以风险接受放行。** 按 §5，本轮预算已用完，停止开放式扫描，转为该项的明确修复任务，不自动另开同样的评审循环。

**最终本片结论：FAIL。** 本报告不提升为全量 C 验收结论。