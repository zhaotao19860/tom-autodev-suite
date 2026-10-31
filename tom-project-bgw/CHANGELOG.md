# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-10-31

- `references/runtime-topology.md`：补 `get_bgw` 参数映射与上游参数规则；记录 autodev 测试分支的发布约定——提测由需求负责人在平台跳过，x86bgw 348101 的测试准出随之自动通过，bgwagent 348141 的测试准出为人工确认、跳过不算准出（发布会被拒“没有执行准出，不能发布”）。

## 2026-10-25

- 说明空 `manualParams` 不等于没有 job 输入：`get_bgwagent` 可继承上游静态默认值。按 run 冻结的变更状态解析：未变更模块取
  目标分支 BranchPipeline 的成功发布，已变更模块取本 run 精确候选的 ChangePipeline 编译产物，revision/分支可核验且可下载；
  job 覆盖需已证实的契约，不支持时显式阻断（`JOB_PARAMETER_OVERRIDE_UNSUPPORTED` / compile-only 触发）。不把 `params={}`
  报告为动态解析，也不把固定旧节点当作本 run 产物。`references/runtime-topology.md` 同步。
- 控制面行为（本 skill 文本未改）：未变更模块的发布 revision 必须等于冻结 revision（`PRODUCT_REVISION_MISMATCH`）；
  这些人工阶段参数类失败属环境/平台类，诊断不能走 REPAIR（`ENVIRONMENT_REPAIR_FORBIDDEN`）。

## 2026-09-15

- 补充 BGW 25G 测试拓扑、关联日志、版本/统计查看，以及 `P0新case回归` 的 `get_bgw_test_case` / `get_bgwagent` 参数映射
- 明确该阶段只替换 client `tool/bgwagent`，不调用 iPipe，不替换 server BGW

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28449`
- 发布范围由空间可见调整为广场可见
