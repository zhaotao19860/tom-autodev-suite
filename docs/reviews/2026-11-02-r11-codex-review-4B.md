# 套件评审记录 2026-11-02-r11-codex-review-3B

**结论先行：本片 FAIL。** `R11B-001` 的原始 `128 + Empty reply from server` 场景已修复，但同根因的发送后失败变体仍会被 `_PUSH_REFUSED` 误判为拒绝，故该 P1 **未关闭**。未新增独立 `R11-` finding，按第 3 节合并至原 ID。

- 标准：`TAD-REVIEW-EXIT / 1.0`
- 标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 仓库/分支：`tom-autodev-suite / mvp`
- `base_commit`：`e41578cdddc09f013844c83acd2aceccd3930559`
- `target_commit`：`cf27f02d3e975932483d46fa14d229cae717cc6f`
- 目标层级/范围：C 局部定向复核；仅检查 `R11B-001`、iCode `push_cr` 状态机及相关回归
- workflow/profile/schema/skill：本项未改变，继承上一轮身份；未调用真实平台
- 评审者/模型版本/日期：独立评审；模型版本未知；2026-11-02
- 负责人：`tom`；方式：第 5 节第 3 条定向复核，本片 1 次

## 验收矩阵

| 场景 | 证据、实际结果 | 状态 |
|---|---|---|
| `128 + Empty reply` | 内存桩走 `tom-autodev/scripts/clients/icode_runtime.py:424` 分支：首次 `SUBMIT_RESULT_UNKNOWN`；重放为 `QUERY_REQUIRED`；`push_calls=1`。对应回归位于 `tom-autodev/scripts/tests/test_icode_runtime.py:739` | 通过 |
| RPC reset / timeout | 非零退出且不匹配拒绝词时保持 UNKNOWN；已有用例位于 `tom-autodev/scripts/tests/test_icode_runtime.py:716`、`tom-autodev/scripts/tests/test_icode_runtime.py:728` | 通过所列场景 |
| 未知路径只读核对 | 已有 intent 在 `tom-autodev/scripts/clients/icode_runtime.py:321` 进入 `_submit_verdict`，不会再次执行 `push_cr`；内存桩重放结果为 `SUBMIT_RESULT_UNKNOWN → QUERY_REQUIRED`，`push_calls=1` | 通过 |
| 明确拒绝及修正后重试 | `missing Change-Id` 进入 `SUBMIT_REJECTED`；持久化拒绝回执后由 `tom-autodev/scripts/clients/icode_runtime.py:296` 生成 `:after:` 新 key，内存桩结果为 `SUBMIT_REJECTED → OK`、`push_calls=2`；回归位于 `tom-autodev/scripts/tests/test_icode_runtime.py:681` | 通过 |
| 发送后失败含权限类文案 | `returncode=1`、输出 `objects sent; response read failed: Permission denied` 命中 `tom-autodev/scripts/clients/icode_runtime.py:988`，实际走 `SUBMIT_REJECTED` 并允许后继 attempt | **失败** |
| 完整 unittest | `python3 -m unittest tom-autodev.scripts.tests.test_icode_runtime` 启动了 53 个用例，但均因沙箱无可写临时目录而在 `setUp` 报 `No usable temporary directory` | 未验证 |

## Findings

| ID / 严重级别 | 违反合同 | 触发、预期/实际、影响 | 当前证据及反证核对 | 处置与复核状态 |
|---|---|---|---|---|
| **R11B-001 / P1** | WF-04/05；MVP §3.1 | 原始 `128 + Empty reply` 现在应保持 UNKNOWN，已符合。可是发送已发生后若非零输出含 `Permission denied`、`not permitted` 或 `invalid argument/option`，仍被当成明确拒绝；随后可重推，存在重复 CR/重复写入风险 | `tom-autodev/scripts/clients/icode_runtime.py:425` 仅按整段 stdout/stderr 正则判断，没有发送阶段或权威回执字段；`tom-autodev/scripts/clients/icode_runtime.py:431` 写入拒绝回执；`tom-autodev/scripts/state_store.py:1919` 允许新 attempt。内存桩已复现 `post-send-permission → SUBMIT_REJECTED` | **未关闭，部分修复**。应让已进入 dispatch 后的所有非零退出默认 UNKNOWN；仅接受结构化、权威的远端拒绝，或有明确 `before_dispatch` 证据的本地失败。补充权限类发送后失败的“无拒绝回执、只读核对、不得重推”回归 |

新增独立 `R11-` finding：无。上述风险与 `R11B-001` 同合同、同根因影响，按标准合并，不重复编号。

## 剩余风险与改进

| Finding/限制 ID | 范围与影响 | 绕行/理由 | 负责人及接受决定证据 | 到期/重开条件 |
|---|---|---|---|---|
| `R11B-001` | iCode `push_cr` 失败分类仍可能释放写入意图 | 当前只能避免不含拒绝词的丢应答；不能安全覆盖权限类文案 | `tom`；无风险接受记录 | 修复分类证据模型并完成定向回归后重开 |
| `LIMIT-001` | 完整 unittest 未能运行 | 当前沙箱所有候选临时目录不可写；已用内存桩和静态路径取证 | 环境限制，非业务平台证据 | 在可写环境补跑目标 unittest |

## 结论

- C：**BLOCKED**，依据：`R11B-001 / P1` 仍存在可重推路径。
- M：未验证；未执行模型/skill 资格场景。
- P：未验证；未调用真实 iCode 或业务平台。
- 局部复核结论：**FAIL**。原始 `Empty reply` 触发关闭，但本项整体未关闭。
- 未验证项与下一步：收窄 `_PUSH_REFUSED`，最好改用结构化 `before_dispatch` / `remote_rejected` 事实；补充发送后权限类失败回归，再复核。
- 停止/重开原因：按第 5 节第 4 条，确认 P1 未解决，停止开放式扫描，转为有界修复任务。

**本片结论：FAIL**