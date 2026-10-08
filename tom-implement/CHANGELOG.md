# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-10-08

- G5“需修改”原地重做：从被驳回草案和已有提交出发只改意见所指；意见改变已批准需求时不改
  代码，以 `USER_REQUIREMENT_CHANGED` 上报 producer block 并列出旧候选提交。

- 第 2 步要求使用控制面 `prepare-change-set` 生成 Change Set：从任务自有干净 HEAD 提取完整
  Git diff，不手工拼 patch 或哈希，不传任意仓库路径。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28444`
- 发布范围由空间可见调整为广场可见
