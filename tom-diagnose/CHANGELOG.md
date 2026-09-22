# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-09-22

- 对齐修复后的控制器：REPAIR 提案尚无补丁时使用 `repair_diff_hash=null`；G6 审批提案，PLAN/IMPLEMENT 生成候选后重新取得 G5。
- 源码 Review 诊断的 build/stage/job 与环境字段为 null；流水线诊断保留真实失败身份，并校验 stage/job 归属。更新阶段合同，删除已解决的身份合同与非空 diff 限制。

## 2026-09-21

- 内置 tom-autodebug 1.5.0 的 relay/tmux、多机角色、TTL、进程取证脚本及原回归；保留原配置/状态目录以兼容已有用户，不再调用外部 skill。
- 分开流水线 diagnosis DraftContent 与独立远程取证报告；补根因证据、反证、时间线、清理和不支持的 transport 边界。
- 修复 Producer/Envelope 分工，明确当前 REPAIR 无 diff 时的 schema/controller 冲突，禁止虚构 hash 或越阶段写修复。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28446`
- 发布范围由空间可见调整为广场可见
