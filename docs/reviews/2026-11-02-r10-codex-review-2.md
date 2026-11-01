# 局部变更复核（修复复核，第 2 轮）

- 标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`，已核对。
- 仓库/分支：`tom-autodev-suite / mvp`；工作区干净。
- base_commit：`eb9ed7c981cff557e3098963459d1d354b61c085`
- target_commit：`4e7417dc67eae613844c5478a92bb669795ccff8`
- 范围：指定差分的 10 个文件及修复调用方，仅局部 C 复核；环境为当前 macOS、Python 3.14.4。
- 规则身份：workflow canonical hash 为 `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；profile/schema/skill 取固定 target，未发生差分；prompt/model 资格不适用。
- 评审者：独立 Codex 上下文，精确模型版本不可见；日期：2026-11-02。负责人：评审发起人或其指定维护者；本轮为第 2 轮。
- 继承线索：`docs/reviews/2026-11-02-r10-codex-review-1.md:1`；四项均重新执行失败场景对照，不直接沿用旧结论。

## 验收矩阵

| 不变量/场景 | 当前证据与实际结果 | 状态 |
|---|---|---|
| WF-04、WF-08：profile 变更后的 build 处置 | 两个 build 均按各自持久化 module/revision/pipeline 查询；仅产生 GET，查询未写状态；运行中、不可读 build 仍阻断；重开后源保留 H0、目标钉住 H1 | 原场景通过；新增边界失败，见下文 |
| WF-02、WF-04：G8 本地错误重发 | 真实配置 B 被放弃→修改为 A→本地调用错误→原 G8 重发：base 为 `APPROVAL_INPUT_MISMATCH`，target 为 `OK` | 通过 |
| WF-02、WF-04：消费后生成新 G8 | 成功执行与普通放弃交替消费四次，每次获得不同 hash；本地错误仍只允许一次重发 | 通过 |
| WF-02、WF-08：历史 v4 与伪造 hash | 历史缓存 action 用原 G0 完成至 GRILL；36 组 matcher 对照接受结果一致；绑定字段篡改被拒，篡改后重算 hash 也不能复用原 G0 | 通过 |
| WF-10：fixture 采集兼容 | 现存 11 个录制目标经真实客户端方法、回放 transport 全部完成；历史 404 fixture 保留 | 通过 |

验证共 **43 项选定现有 unittest 通过**，其中 37 项使用内存 SQLite/虚拟文件夹具，6 项直接通过 unittest 命令运行；另执行了历史提交及 CLI 调用链对照。未运行业务项目编译/测试，未调用真实平台，未修改仓库文件。

## Findings

### R10-NEW-001 · P2：无 build 的 profile 变更重开，被不必要的 iPipe 认证初始化阻断

**位置：** `tom-autodev/scripts/orchestrator.py:4942`  
**调用方：** `tom-autodev/scripts/orchestrator.py:3612`  
**违反合同：** WF-04，以及 `docs/OPERATIONS.md:232` 承诺的 profile 变更恢复路径。

**失败场景：**
1. 源 run 仍在 INTAKE，没有 build、未决 intent、CR 或工作树遗留。
2. 将同路径 profile 的 `submission_policy` 从合法 H0 改为合法 H1。
3. 操作者用户名可解析，但没有 `COMATE_AUTH_TOKEN`，也没有本地登录凭据。
4. 执行 `restart-from SOURCE_RUN`，仅查看重开计划。

**预期/实际：** 无 build 就不需要查询 iPipe，应该返回 `RESTART_READY`。实际在调用 `restart.plan()` 之前，无条件执行 `IpipeHttpTransport()`；`tom-autodev/scripts/clients/ipipe_client.py:442` 抛出 `ValueError("IPIPE_AUTH_REQUIRED")`，异常直接逸出 CLI。

**当前证据及反证核对：**
- 同一内存状态、同一环境调用真实 CLI `main()`：base 输出 `RESTART_READY`；target 抛出未捕获的 `IPIPE_AUTH_REQUIRED`。
- `status()` 内的异常处理始于 `tom-autodev/scripts/orchestrator.py:4949`，覆盖不到提前执行的 transport 构造；调用方也没有“无 build 则不初始化”的分支。
- 已验证：该无 build 场景通过 Python API 调用 `restart.execute()`、不传 `build_status`，可正常得到 `RESTARTED`。因此是有可行绕行的局部恢复回归，定为 **P2**。

**建议修法：** 将 API/transport 初始化延迟到 `status(build_id)` 内，在确认存在该 build 的持久化 binding 后执行，并纳入查询异常处理。无 build 时不触碰 iPipe 认证；有 build 而认证不可用时返回未知状态，由现有守卫返回 `RESTART_SOURCE_UNRESOLVED`。补充上述无 build、profile 漂移、缺认证的 CLI 回归。

**处置状态：** 确认，未修复；未见负责人风险接受记录。

### 上轮 finding 关闭状态

| ID / 原级别 | 当前位置 | 复核证据 | 状态 |
|---|---|---|---|
| REC-01 / P2 | `tom-autodev/scripts/orchestrator.py:4919` | 原失败场景已恢复；按 build 自身 binding 只读查询，正常执行入口仍返回 `PROFILE_CONFLICT` | **关闭** |
| FIX46-001 / P2 | `tom-autodev/scripts/clients/ipipe_runtime.py:1046` | B 放弃→A 本地错误→原 G8 重发为 `OK`；46ca687 的消费后新 hash 保证仍成立 | **关闭** |
| R9-NEW-001 / P2 | `tom-autodev/scripts/collaboration.py:154` | 使用 3ff3ee1 的 INTAKE/action 生成代码构造历史缓存；base 的 next、只读 next、complete 均冲突，target 保留原 action 并用原 G0 推进至 GRILL | **关闭** |
| REL-001 / P2 | `tom-autodev/scripts/tests/fixtures/platform/record.py:134` | 旧末项目复现 `AttributeError`；target 移除该调用，剩余 11 个目标均通过真实客户端方法验证 | **关闭** |

`_intake_keys` 的兼容识别不是提交来源认证：自行重算出匹配 H5 的 v4 payload 仍可通过自洽性检查，但这一接受集合在 base 已存在；本次未扩大集合，且已验证不能据此复用原 G0，故不另列缺陷。

## 剩余风险与改进

| Finding | 范围与影响 | 已验证绕行 | 负责人及接受决定 | 到期/重开条件 |
|---|---|---|---|---|
| R10-NEW-001 | profile 漂移、无 build、缺 iPipe 认证时，CLI 重开计划异常退出 | 仅对确认无 build 的源 run，通过 Python API 重开，不传 build 查询回调 | 发起人/指定维护者；待决定 | 转有界修复或按第 3 节完整记录接受决定；不自动开启同批次第三轮 |

## 结论

- 上轮四项 finding **均已关闭**；本轮确认新增 **1 项未处理 P2**。
- 全量 C、M、P 均未验证，不能由本次局部复核推导通过。
- 按第 5 节，第 2 轮仍有阻断项，停止本批次开放式评审，转明确修复/风险处置任务。

**局部复核结论：FAIL（标准状态：BLOCKED）。**