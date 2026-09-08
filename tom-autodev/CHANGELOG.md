# 发布记录

本 skill 属于 tom-autodev 套件，整套共 13 个，必须一起安装才能完成端到端流程。套件构成与各自职责见 SKILL.md 的依赖说明。

## 2026-09-08

- 修复 iPipe 多模块证据聚合、build/stage ownership 持久化、旧 revision 误用和 stage endpoint fallback 问题；补充 module/build/stage 证据引用及按 module 配置 release rule
- 修复 watcher、审批提醒、超时重发、确认回执和 IDE handoff 通知的并发重复发送；未知外部结果统一进入查询态
- 修复状态迁移与幂等写入的原子性，增加旧版 Review/Change Set artifact 兼容校验
- 增加 `ipipe-rerun` CLI，修正 Comate Stop hook 文档，并让 Infoflow 协作群在 bot agent 缺失时快速失败
- 控制面回归测试 `628` 项全部通过

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28434`
- 发布范围由空间可见调整为广场可见
