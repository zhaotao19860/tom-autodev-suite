# 第 5 轮定向修复复核 · 分片 B

**结论：FAIL。R11B-001（P1）未关闭。** 原 `Permission denied` 触发及相关文案已修复，但剩余正则仍将**单个推送目标的拒绝误当成整次操作未落地**，允许部分成功后的再次推送。此为原 finding 的同根因残留，无新增独立 `R11-` finding。

## 基线与范围

- 标准：`TAD-REVIEW-EXIT / 1.0`；已核对 SHA-256：  
  `5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 仓库/分支：`tom-autodev-suite / mvp`
- base_commit：`cf27f02d3e975932483d46fa14d229cae717cc6f`
- target_commit：`52128a7ed2f498863e29733c4eaa24a2324d0852`
- 范围：C 局部复核，仅 R11B-001、拒绝分类 diff 及提交恢复调用链。
- 身份：workflow=`workflow-spec-v2`；`tom-autodev/SKILL.md` SHA-256=`3d92c2131331cfc9bc987dcad2dae2357947b1602966b83e584560ece19d4f72`；业务 profile、schema/prompt 资格不适用。
- 评审者：Devin，模型版本未知；日期：2026-11-02；负责人：tom。
- 轮次：用户指定第 5 轮，本片执行一次；继承 `2026-11-02-r11-codex-review-4B.md` 的场景，重新核验当前代码。

## 验收矩阵

取证使用 `PYTHONDONTWRITEBYTECODE=1 python3 -B -c '…'` 内联脚本：内存 SQLite 保留真实状态存储、审批、intent/receipt 逻辑；注入 transport 和本地 Git/工作区观察，不运行磁盘夹具。

| 场景 | 当前证据与实际结果 | 状态 |
|---|---|---|
| 原权限文案及同根因变体 | 共 12 种：权限、not permitted、invalid argument/option、prohibited、裸拒绝标记，以及 Empty reply、RPC reset、124、信号和空输出。目标均 `SUBMIT_RESULT_UNKNOWN → QUERY_REQUIRED`，`push_calls=1、receipts=0、pending=1`；CR 可见后只读收敛为 OK。前 7 种在 base 均误拒绝且推送两次 | 通过 |
| 真实拒绝与修正后重试 | 两种 Gerrit 拒绝、missing Change-Id、repo-name 失败均结束本次 attempt；修正后成功，合计两次推送 | 通过 |
| 部分成功后出现 Gerrit 拒绝 | 两个 push URL：A 接受、B 拒绝，列表暂未显示 A 的 CR。真实 `submit` 路径复现误拒绝并再次 dispatch，见下文 | **失败** |
| 本地检查是否可能后置 | 当前已安装 CLI 静态路径中，repo-name 提取失败早于 push；runtime `:1145–1146` 也在 claim 前检查缺 Change-Id。未确认“此 CLI 推完再做这两个本地检查”，不将任意注入文本当成可达性证据 | 所列路径通过 |
| 相关 diff | runtime 仅收窄正则，新增测试未覆盖部分成功；相关 `git diff --check` 退出 0。未发现新增独立缺陷，仍是原项残留 | 部分通过 |

## Findings

| ID / 严重级别 | 违反合同 | 触发、影响 | 当前证据及反证核对 | 处置 |
|---|---|---|---|---|
| **R11B-001 / P1** | WF-04/05；`docs/MVP_CONVERGENCE_PLAN.md:88–96`：结果不明只能只读确认，不能直接重发 | HTTPS origin 的多个 `pushurl` 中，A 已接受推送，B 返回 `! [remote rejected]`，命令非零退出。应保留 UNKNOWN；实际写拒绝 receipt、清除目标占用，列表延迟时允许重推 | `tom-autodev/scripts/clients/icode_runtime.py:425–438、993–994` 对整个输出做任意命中；`:296–305` 为拒绝回执建立后继 key。`state_store.py:985–993、1919–1922` 不再将旧 attempt 视为 pending。新 attempt 的查询能在 CR **已经可见**时防重，但不能覆盖列表延迟 | **确认未关闭，部分修复** |

### 可达性与复现证据

这不依赖臆造的 Gerrit 单目标多 ref 行为：

- 已安装 `icode`/`icode-cli` 字节相同，SHA-256：`68a05ea326b50b4bc3eb1a039ae0b13e2743bca928c1e9164e197b105c65fb32`。静态反汇编确认实际调用为 `git push origin（或 origin-https） HEAD:refs/for/<branch>`。
- 本机 `git-config.1:9728–9734` 明确：多个 `pushurl` 会全部推送。只读 `git -c … remote get-url --push --all origin` 验证能解析两个目标，未执行 push。
- runtime `:588–605` 未限制多个 push URL，临时 clone 也未隔离全局 Git 配置；审批、工作区与分支漂移守卫没有排除此配置。

内存复现结果：

```text
第一次：SUBMIT_REJECTED，push_calls=1，pending=0，states=[REFUSED]
重放：  SUBMIT_REJECTED，push_calls=2，pending=0，states=[REFUSED, REFUSED]
CR可见：OK，push_calls仍为2
```

以上是模拟结果，**不声称真实平台创建了第二个 CR**；已证明错误终结意图并重复发送的路径。

**建议修法：**拒绝证据必须绑定准确目标，并证明整次操作未落地；无法证明时保持 UNKNOWN。可明确限制并校验实际推送目标，或提供结构化逐目标结果。补充“部分成功＋拒绝＋列表延迟”的回归，不能仅继续替换关键词。

## 剩余风险与改进

| ID | 范围与处置 | 负责人／接受决定 | 重开条件 |
|---|---|---|---|
| R11B-001 | P1 阻断，转有界修复任务；不能以预算耗尽放行 | tom；无接受记录 | 完成整次操作拒绝证明及对应回归 |
| 验证限制 | 未运行磁盘 unittest、业务编译测试或真实平台；工作区未修改 | 环境及用户约束 | 必要时在允许环境补对应回归，不扩大本片范围 |

## 结论

- C：**BLOCKED，仅指本片受影响不变量**，不代表全量 C 重评。
- M：未验证。
- P：未验证。
- 局部复核：**FAIL，R11B-001 未关闭**。
- 按第 5 节停止开放式复核，不追加轮次；下一步为上述有界修复。

**本片结论：FAIL。**