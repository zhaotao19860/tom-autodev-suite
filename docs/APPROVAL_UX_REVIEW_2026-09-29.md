# 审批 UX 局部复核记录

- 标准：TAD-REVIEW-EXIT / 1.0。
- 标准 SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库：`tom-autodev-suite`；分支：`phase1-workflowspec`。
- base / 当前 HEAD：`c21494d24d2f1e40fa6d622e197056e0b0ecca2f`。
- target：本轮未提交的审批 UX 工作区改动，无正式 target commit。
- 范围：G0–G10 / PROFILE_REPIN 的说明和材料定位、请求投递、单聊/群聊按钮、进度通知，以及与这些变更直接相关的恢复和授权回归。
- 不纳入：前序 profile wizard 实现、业务 syncookie 修复、真实平台测试，以及用户原有 `tom-project-bgw/references/runtime-topology.md` 改动。
- 负责人：用户发起维护；未代其作平台验收或风险接受。
- 执行者：主会话及独立上下文检查；精确模型版本在执行环境不可见。
- 预算：一次独立初查合并、一次定向修复与复核；不继续开放式扩展评审。

## 验收证据

| 项目 | 本轮证据 | 状态 |
|---|---|---|
| 每个门说明审什么、重点、材料、授权边界 | `test_approval_materials` 的全部 gate/retry 文案与 G0/G5/G9 渲染回归 | 已验证 |
| G0 实际 profile、目录、路由和可读全文 | 临时 run 的实际文件读取；不创建审批、不推进状态；错误 G0 hash 拒绝 | 已验证 |
| producer 材料与 G5 候选绑定 | 由当前 action 重建 envelope，候选 hash 不符返回缺口 | 已验证 |
| WORKSPACE G4 材料 | `test_workspace_approval_materials`：真实临时仓/controller、ACTIVE ownership、精确 binding hash、只读取材及可打开副本 | 已验证 |
| G9 完整固定证据 | 精确引用两模块产物及匹配冻结计划；错误 hash 拒绝 | 已验证 |
| G10 proposal | 从实际 state API 按确定性 proposal ID 读取并验证 run/candidate/envelope；篡改拒绝 | 已验证 |
| 无 pending 不要求人批准 | `test_approval_materials`、`test_run_brief`、`test_approval_watch` | 已验证 |
| 单聊/群聊按钮和组件限制 | 实际网关源码与已安装 SDK 的离线 harness，替换所有网络、凭据读取及 journal 写入；目标独立 ID，7+1 组件 | 已验证（离线） |
| 未知发送、重复请求、已知未发送恢复 | `test_approval_delivery`；已发送/未知不盲重试，接口预校验失败可修复后重试 | 已验证（离线） |
| 权限、有效期、重复/冲突回应 | 既有 `test_infoflow_reply_consumer` / `test_approval_channels` / `test_approval_gate_retry` 与新增网关点击提示回归 | 已验证（离线） |
| 真实如流渲染、送达与点击 | 未发送真实消息，未重启运行中的网关 | 未验证 |

## Findings 与处置

| ID | 级别 / 合同 | 确认触发及修复 | 复核 |
|---|---|---|---|
| UX-01 | P1 / WF-02、WF-04 | 未知投递后 next 变为 RECOVERY_REQUIRED，动态材料从 PINNED 变 MISSING 导致 payload 冲突。改为冻结首次 context；旧 CLI 形状仅在完整 channel fingerprint 精确一致时复用。 | 真实 request→未知发送→reconcile 回归通过；独立定向复核关闭 |
| UX-02 | P1 / WF-02、WF-10 | 显式文件可拷贝自报 approval hash，展示 foreign run/其他内容。现按当前 run/action/task/版本重算，原始 envelope 才标 PINNED。 | 跨 run、换内容并自改 content hash、换 task 均拒绝；独立定向复核关闭 |
| UX-03 | P2 / 材料可读要求 | WORKSPACE G4 binding hash 不等于普通 action hash，原逻辑遗漏已有材料。新增只读 ACTIVE ownership 读取并复用真实 binding 校验。 | 6 项专项及已有 controller 回归通过；主会话核对实现 |
| UX-04 | P2 / WF-04 | transport 永久 intent 把缺方法/签名错误这类未发送故障变为永久未知。首次 claim 前验证选中的发送方法可调用性及参数签名。 | 私聊/群聊 × 3 类本地接口错误共 6 子场景修复；方法体异常仍保留未知保护 |

第一份初查未发现问题；第二份独立初查复现 UX-01/02/03。以后者的具体证据为准，不以“另一份没发现”否定已确认缺陷。修复后只复核对应问题与影响面。

## 验证运行

在套件根目录运行：

```bash
python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_*.py' -q
node --check tom-autodev/infoflow-gateway/src/bot_gateway.mjs
git diff --check
```

联合回归从 `tom-autodev/scripts/tests` 运行：

```bash
python3 -m unittest test_approval_materials test_workspace_approval_materials test_approval_delivery test_infoflow_gateway test_infoflow_reply_consumer test_approval_channels test_approval_gate_retry test_approval_watch test_run_brief test_progress_snapshot test_profile_repin test_cli_operations test_workflow_spec -q
```

- 修复前的整批控制面回归：1087 项通过，378.217 秒，退出码 0。这不是后续定向修复的最终证据。
- UX-01/02 定向材料回归：12 项通过，3.544 秒；独立上下文再运行 12 项通过。
- WORKSPACE G4 专项：6 项通过；相关联合回归 40 项通过。
- 已知未发送恢复修复：相关 delivery/授权/持久化联合回归 144 项通过。
- 最终审批交互联合回归：233 项通过，36.217 秒，退出码 0。
- 最终 tom-autodev 全量控制面回归：1100 项通过，377.627 秒，退出码 0。包含本轮全部定向修复。
- Node 语法检查、`git diff --check`：通过。
- Python 3.14 输出了 SQLite/HTTPError 的 ResourceWarning；没有把这些告警隐藏或算作已修复。

本轮最终生产文件 SHA-256（包含该文件在工作区中的全部内容，非正式 commit 身份）：

```text
bb7273a72473d1336176d116acae04663c86bdd25e74316a9b5914c55e1eb16a  tom-autodev/scripts/approval_summary.py
1b38af0d42ed45bad520e50b49c1190f789f61f4b2db9ad623f8b04d63ec89ba  tom-autodev/scripts/approval_delivery.py
22e520decd8c1e813685362f03f31f9f3dbc3eee5ee281c555b76285503aae21  tom-autodev/scripts/approval_watch.py
c400bb6700889ff56568faee5ecd5d53f387c2a579985459275c091cf6d2badf  tom-autodev/scripts/orchestrator.py
1fbecf4f0e3d36bdaba8a9570e7c7bd9e26ea2c9f2f51aa58ed0e81089d89a20  tom-autodev/scripts/profile_repin.py
f46029d04d46349956dce3c9dd793871777b72dd59b358d9570753c0ed29003a  tom-autodev/scripts/run_brief.py
af5a653d697e7f2ccf802c002aff2d7cbcfee8a36a4a27c6ae6f31740f13a8a8  tom-autodev/scripts/progress_snapshot.py
080eca7e62b8f42ce331655d1ea09af7eb827b3f85e94d0a78a2cac4d453627e  tom-autodev/infoflow-gateway/src/bot_gateway.mjs
```

## 限制与结论边界

- 本地路径只在运行机器可打开；现有 KU 链接照常展示，不伪造远程材料链接。
- 手工 G7 trigger / G8 需提供匹配 hash 的 binding 与证据文件；找不到精确材料时明确提示请勿批准，不拿最新文件替代。
- 按钮点击只显示待校验；终态以账本及 watcher 回执为准，尚无 ledger-driven 卡片终态同步，不能把保留的按钮误解为仍可覆盖已生效决定。
- 按钮降级通知为 best effort；通知自身断网/崩溃可能无法到达，持久化结果仍可检查。
- 有 intent 而无回执的历史投递保持未知，不能自动清理。修复不会把历史未知伪装成未发送。
- C：不作全套件正式验收结论（未提交目标、仅本轮局部范围）；M、P：未验证。
- 最终局部结论：本轮本地实现与离线回归通过，已确认的 UX-01/02/03/04 均有对应修复及复核证据。该结论不提升为正式 C/M/P 验收。本轮未推进 BGW-1995、未新增审批或批准、未修改其 profile；未提交或发布套件。
