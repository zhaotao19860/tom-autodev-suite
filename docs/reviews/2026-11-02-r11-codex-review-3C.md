# 套件评审记录 R11-C／第 3 轮

**结论先行：本片 FAIL，C：BLOCKED。上一轮两项 finding 的原失败场景已修复，但均存在已复现的同根因残留，不能关闭；未确认其他新增独立 finding。**

## 基线与范围

- **标准与代码**：已读 `AGENTS.md:1`、`docs/WORKFLOW_EXIT_CRITERIA.md:1`；标准 `TAD-REVIEW-EXIT / 1.0`，SHA-256 核对一致：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。仓库／分支：`tom-autodev-suite / mvp`；base_commit：`01f123ad21f347bdeddabc6e3e10eb40d0558ce2`；target_commit：`e41578cdddc09f013844c83acd2aceccd3930559`，HEAD 匹配、工作区干净。
- **规则身份**：workflow `workflow-spec-v2`，canonical hash：`76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`；evidence contract `v2`；schema Git tree：`b009dbae263cafb7ef1b4c07ddf69fc8130b358a`；`tom-autodev/SKILL.md:1` SHA-256：`3d92c2131331cfc9bc987dcad2dae2357947b1602966b83e584560ece19d4f72`。本轮内存 profile 由 `test_live_preflight.PROFILE` 改写路径，字节 SHA-256：`772d52c0ef4cb903784dcd2c2d2c2ce08efadfcb3b0f1d51f3ffe63ac88c2b97`；未执行模型 producer prompt。
- **范围与责任**：C 第 3 轮、分片 C，定向检查两项遗留及相关 diff／调用方，不重扫全仓。继承来源：`docs/reviews/2026-11-02-r11-codex-review-2C.md:1`；其中旧七项关闭记录不提升为本轮完整 C 通过。评审者 Codex，具体模型版本未知；2026-11-02；环境 Darwin arm64、Python 3.14.4。负责人 tom；修复→复核已用 **2/2**，本轮为授权最后一轮。

## 验收矩阵

| 场景／合同 | 当前复核证据 | 状态 |
|---|---|---|
| R11-C-001 原场景／WF-03、09 | T-1 已通过 Review，T-2 依赖 T-1。base 连续第二次 REVISE 后真实 `next` 返回 T-2；target 连续两次均返回 `OK / PLAN / T-1`。草案校验、审批结算、revise 均为 OK；未 Mock `_current_draft` 或审批 hash。 | **原触发关闭；finding 未关闭** |
| R11-C-002 原场景／WF-10 | legacy 有效、需求文件不存在时，真实 CLI 分别传 `--card BGW-1995`、`--card ../wrong`，均返回 `PROJECT_NOT_READY / 退出 1`，探针调用 0 次；准确需求文件存在时返回 `READY / 0`，六个探针各一次，数据库不变。 | **原触发关闭；finding 未关闭** |
| 修复影响面与正常路径 | 内存适配执行 `test_run_change` 3 项、`test_live_preflight` 5 项、`test_scope_worker` 1 项，均通过；原样执行 `python3 -B -m unittest test_cli_operations.CliExitCodeTests -v`，1 项通过；`git diff --check 01f123a..e41578c` 退出 0。 | **仅上述检查通过** |

## Findings

| ID／严重级别 | 违反合同与文件:行 | 失败场景、证据及反证核对 | 处置与建议修法 |
|---|---|---|---|
| **R11-C-001／P1** 连续重做仍会丢失修复对象 | WF-03、WF-09；`tom-autodev/scripts/run_change.py:210`；下游 `tom-autodev/scripts/phase_protocol.py:1114` | 同一修复 PLAN 连续执行 **33 次合法 REVISE**：前 32 次 `next` 均为 T-1，第 33 次返回 **`OK / PLAN / T-2`**，而最新事件仍记录 `task_id=T-1`。33 次草案校验、审批结算、revise 全部成功，审批台账 33 项均为 REVISE。根因：每次 `repair_of` 指向上一事件，但链识别只追溯 32 层；超限后按“已经评审”跳到下一任务，而不是阻断。**反证**：现有新增回归仅覆盖两次重做，不能排除此边界。 | **部分修复、未关闭**；与 R11A-003 去重。保存经验证的原始修复入口，或以防循环遍历验证完整链；链无法证明时应阻断，不得切换任务。补第 33 次重做回归。 |
| **R11-C-002／P2** 显式空 card 仍静默回退 legacy | 显式 card 只检查准确需求 profile 的合同／WF-10；`tom-autodev/scripts/orchestrator.py:675` | legacy 有效、无需求文件，真实 CLI 执行 **`preflight bgw --card ''`**：实际 **`READY / 退出 0`，六个探针各一次**；预期非法 card 应拒绝且不执行探针。`if card` 把显式空串当成未指定，绕过 `requirement_profile_path` 的非法值校验。**反证**：parser 接受空串；新测试只覆盖非空缺失／非法值，没有覆盖此分支。该命令不直接写远程对象，维持 P2。 | **部分修复、未关闭**。使用 `card is not None` 区分“省略”和“显式传值”，显式空串交由同一校验拒绝；仅省略参数时读取 legacy。补空串 CLI 回归。 |

## 剩余风险与改进

| ID | 范围与处置／负责人决定 | 到期或后续条件 |
|---|---|---|
| 上述两项及 LIM-C | tom；无风险接受决定。P1 不得放行；P2 的非空准确 card 正例已验证，但绕行不等于接受。只读沙箱禁止 `/tmp` 写入，复现通过 stdin、内存 SQLite 和文件／平台 Mock 完成，**未落盘临时脚本，真实文件系统型回归未验证**。未修改仓库、未运行业务编译／测试、未调用真实平台。 | 由 tom 转为明确的有界修复任务，并在可写隔离环境补对应证据；不得用本报告宣称完整 C 通过。 |

## 结论

- **C：BLOCKED（本片）；M：未验证；P：未验证。** 按 §5，本轮预算已用尽，停止本批次开放式评审，保留上述未解决项及证据缺口。**本片结论：FAIL。**