# 阶段产物与知识库协作文档

ArtifactStore 是控制面的规范事实源，保存完整 canonical JSON、diff、日志引用、
hash、审批绑定和外部 receipt。知识库是团队阅读、评论和交接的协作界面，不能只
发布阶段名、字段摘要或原始 JSON。

## 每个阶段都要回答的问题

每个写入 KU 的阶段文档必须按统一结构组织正文：

1. 一句话结论
2. 本阶段要解决什么
3. 上游输入
4. 实际内容
5. 本阶段确认了什么
6. 产出物
7. 与上下游的关系
8. 风险、缺口与待办
9. 交接清单
10. 审计与证据

正文面向需求、开发、测试、评审人和接手同学。文末的规范化 JSON 附录用于机器
核验，不能替代正文。无法从 artifact 证明的内容必须写成“未产生”“未知”或“待
确认”，不能用模板推测事实。

## 00～11 文档目录

| 编号 | 文档 | 主要回答的问题 | 产出来源 |
|---|---|---|---|
| 00 | 需求快照与运行章程 | 这次需求、项目、仓库、人员、分支和运行边界是什么？ | requirement snapshot |
| 01 | 需求澄清与决策日志 | 哪些问题由用户选择确认，验收增量是什么？ | `tom-grill` decision result |
| 02 | 行为规格说明 | 目标行为、输入输出、异常、兼容边界和验收场景是什么？ | `tom-spec` Spec |
| 03 | 任务拆解与验收覆盖 | 任务 DAG、依赖、并行关系和验收覆盖是什么？ | `tom-tasks` Task DAG |
| 04 | 工作区与基线绑定 | 实际仓库、分支、revision、worktree 和锁是什么？ | workspace binding |
| 05 | 任务实现计划 | 准备改哪些文件、符号、接口和测试，iPipe 如何验证？ | `tom-plan` Task Plan |
| 06 | 候选改动说明 | 实际改动是什么，行为如何变化，与计划有何偏离？ | `tom-implement` Change Set |
| 07 | 变更评审报告 | Standards/Spec 双轴评审确认了什么？ | `tom-review` Review Report |
| 08 | iCode 提交与流水线计划 | 提交哪个 revision，触发哪些流水线和参数？ | submission descriptor |
| 09 | 远程验证报告 | build、stage、job、版本、环境和远程结果是什么？ | iPipe evidence |
| 10 | 失败诊断与修复决策 | 现象、证据、根因假设、修复路线和重验计划是什么？ | `tom-diagnose` Diagnosis |
| 11 | 发布验证证书 | 发布了哪个版本，如何证明发布后验证通过？ | release evidence |

## ArtifactEnvelope 最低契约

| Artifact | Producer | Required fields |
|---|---|---|
| Requirement Snapshot | intake controller | card snapshot, project, profile/baseline, participants, collaboration binding, input hash |
| Decision Result | `tom-grill` | result, decision log, acceptance delta when the card had none, glossary delta, evidence refs, input hash |
| Spec | `tom-spec` | version, content hash, behaviors, test interface, environment requirements, traceability |
| Task DAG | `tom-tasks` | nodes, edges, acceptance coverage, completion evidence, G3 hash |
| Workspace Binding | workspace controller | exact repositories, branches, revisions, worktrees, locks, baseline evidence |
| Task Plan | `tom-plan` | exact repositories/files/symbols, interfaces, test cases, iPipe parameters, checklist, G4 hash |
| Change Set | `tom-implement` | business/test patches, complete diff hash, test IDs, traceability delta, deviations from the plan, G5 hash |
| Review Report | `tom-review` | baseline, Standards axis, Spec axis, findings classified `CONFIRMED` / `REJECTED_WITH_REASON` / `NEEDS_CLARIFICATION` with reasons, verdict |
| Submission Plan | submit controller | CR/revision map, target branches, pipeline IDs, allowed parameters, G7 binding |
| iPipe Evidence | iPipe controller | build/stage/job, module, revision map, result, logs/evidence refs, rerun metadata |
| Diagnosis | `tom-diagnose` | reproduction, comparison, class, one hypothesis, minimal verification, route |
| Release Evidence | release controller | release version, revision map, build evidence, release receipt, environment, post-release verification, remaining risks |

Every producer must return the current `run_id`, `task_id` when applicable, `spec_version`,
`input_hash`, source artifact references, and canonical content hash. A consumer rejects
artifacts whose identity or hash differs from the current state.

## 大型内容处理

- 完整 diff、原始 HTML 和大段原始日志只保存在 ArtifactStore。
- KU 中展示文件级摘要、增删行统计、变更原因、测试和受控 artifact/evidence 引用。
- canonical JSON 可以在附录中按字段 hash 省略巨大 payload，但必须保持 content hash 可核验。
- 任何审批、revision、build、iPipe 和外部 receipt 都必须引用真实持久化证据。
