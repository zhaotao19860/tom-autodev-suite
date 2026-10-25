# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-10-25

- 第 4 步：每个 CR 在冻结 baseline 之上只保留一个提交（iCode/Gerrit 拒绝多提交 push，HCK_F507）；resume 或 G5“需修改”
  重做若会产生第二个提交，先 `git reset --soft <baseline>` 再提交一次。`prepare-change-set` 现在以
  `MULTIPLE_COMMITS_ABOVE_BASELINE` 提前失败。
- 新增第 7 步：同一 change-set 内为每个改动源码的仓库追加 changelog 条目，遵循
  `tom-autodev/references/changelog-policy.md`（无 changelog 时从 1.0.0 创建）；缺失即 change-set 不完整。原第 7–9 步顺延为 8–10。
- 控制面行为（本 skill 文本未改）：CODE_ONLY 修复直接回 IMPLEMENT 时，action 的 `baseline_revisions` 现在取已接受 Plan 的
  revision，与首次实现一致。

## 2026-10-08

- G5“需修改”原地重做：从被驳回草案和已有提交出发只改意见所指；意见改变已批准需求时不改
  代码，以 `USER_REQUIREMENT_CHANGED` 上报 producer block 并列出旧候选提交。

- 第 2 步要求使用控制面 `prepare-change-set` 生成 Change Set：从任务自有干净 HEAD 提取完整
  Git diff，不手工拼 patch 或哈希，不传任意仓库路径。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28444`
- 发布范围由空间可见调整为广场可见
