# 子 skill 整合与维护边界

本轮基于本地 `npl-coder`、`code-review-qa`、`tom-autodebug 1.5.0` 和现有套件源码。没有增加 GitHub 下载、外部 skill 安装或业务平台调用。Claude 负责的 workflow 核心 Python/schema 未由本轮修改。

## 本轮落地

| Skill | 完善点 |
|---|---|
| `tom-autodev` | 新增 `references/producer-contract.md`，统一模型与 worker 的边界；不改控制器 |
| `tom-grill` | 只澄清缺失的人类决策，复用已有明确意图；返回 DraftContent |
| `tom-spec` | 同一任务的行为、外部测试边界、独立预期值和覆盖规则；支持 worker 指定的 merged `{spec, dag}` |
| `tom-tasks` | 明确一个 runtime task 绑定一个业务仓；不可独立验证的多业务仓原子任务应报告能力缺口 |
| `tom-plan` | checklist 记录前置条件、动作和完成检查；不冒充已执行日志 |
| `tom-implement` | 恢复时先核对真实 diff/候选，保留完成的编辑；不根据空 checklist 重做 |
| `tom-review` | QA 方法内化；AC 覆盖、误报处置、schema 可接受的 verdict/severity；新增固定提交范围收集器 |
| `tom-diagnose` | 原 AutoDebug 脚本完整内置；工作流诊断与独立远程排障双入口，补可证伪根因和复用证据 |
| `tom-lang-c-cpp` | 生命周期、view/迭代器、边界溢出、UB、并发、ABI 与热路径检查的触发/排除条件 |
| `tom-lang-npl` | 纠正 overlay/赋值位宽错误；区分前端、后端、SDKLT、行为测试；文档索引与来源指纹 |
| `tom-project-xflow` | LT 字段跨层合同、元数据 FieldId、selector/oneof/稀疏 ID、外部读回；镜像历史经验的适用边界 |
| `tom-project-bgw` | GitNexus 改为可选；workspace 从 profile 获取；远程入口迁入 Diagnose；历史拓扑须与当前环境核对 |

子 skill 交回内容，worker 构造 envelope、计算身份、保存回执、执行状态迁移与审批。合并 SPEC/TASKS 的 runtime 模式只需一次模型回合；两个独立知识文件不等于两次调用。Review 两个轴也不要求两次模型调用。

Review 收集器只做 Git 对象/范围读取，包含删除、模式、二进制和特殊路径，禁用 external diff/textconv，不 checkout、不运行项目代码。增删行是辅助范围统计，不是缺陷判断器；没有复制 QA 的方舟/Bug-QA 上传、PRD 检索链或未经适配的全部扫描器。

## 合并与退役建议

| 入口 | 决定 |
|---|---|
| 现有 12 个 tom skill | 保留职责边界。按需加载可减少上下文；继续合成大 skill 不能替代控制器减回合 |
| `setup-tom-autodev` | 已由前一批变更并入 `tom-autodev`，套件数 13→12 |
| `tom-autodebug` | 能力已搬入 `tom-diagnose`，可退役旧的安装入口。原源码仓未删除；保留现有 `TOM_AUTODEBUG_*`、状态目录和 owner 标记，避免已有会话不兼容 |
| `npl-coder` | 套件不再依赖它。语言知识进 NPL，项目经验进 XFlow；原资料/芯片手册应保留归档，不等于都适合塞进模型上下文 |
| `code-review-qa` | 套件不再依赖它。它还有独立多语言审计、报告与平台能力，本轮没有复制，不能据此全局删除 |
| GitNexus / superpowers | 没有变成子 skill 运行时前置条件；已采用的方法写在本地 references |

原来的 Diagnose 主要负责流程失败分类，并没有完整拥有 AutoDebug 远程能力；BGW 拓扑文档另外指向 AutoDebug。现在脚本、进程检查模板、用法和回归都在 Diagnose 内，来源名称仅用于溯源/兼容。`ipipe_runtime.py` 中尚有旧名称的注释，不是运行时调用；本轮为避免干扰 Claude 的控制器工作未改该代码文件。

## 控制器问题的后续处理

1. **DIAGNOSE 在修复前被要求已有修复 diff（2026-09-22 已修复）。** JSON Schema 与语义校验现在都允许 `route=REPAIR` 且 `repair_diff_hash=null`。G6 绑定诊断和修复提案，PLAN/IMPLEMENT 生成实际候选并取得新的 G5。`test_diagnosis_contract.py` 覆盖流水线失败和源码 Review 失败提交无 diff 提案、等待 G6、重启后进入 PLAN；提案通过不等于补丁或业务测试已经完成。
2. **单次运行修复预算没有生产调用者（历史问题，2026-09-22 已接线）。** 原先 `scripts/repair_policy.py::next_action` 只有策略定义；提交 `5973301` 已在 `phase_protocol.py` 的 DIAGNOSE 修复路由中，用持久化历史调用该策略。该提交的回归结果见 [更新记录](CHANGELOG.md)；本段不再作为“未接线”的当前缺陷证据，完整验收仍需验证实际路由和重启场景。

源码 Review 诊断身份合同也已在 2026-09-22 修复：build/stage/job 和环境字段可为 null，控制器绑定真实前驱、task 和冻结 revisions；iPipe 诊断继续核对真实 build、环境、失败签名及所属 stage/job，不能借空值或其他流水线身份绕过。不可拆分的多业务仓 task 仍超出当前单 `business_module` 模型，本轮没有扩大 runtime 能力。

## 验证与后续复用

- 12 个 skill 的 quick validation 通过。
- Diagnose 搬入后的本地隔离回归通过；导入的 4 个脚本/测试文件 hash 与来源记录一致。
- Review 固定提交范围收集器的隔离 Git 测试通过；JSON 示例通过现有 schema 和语义校验。
- 独立只读行为验证覆盖 8 个场景，已沉淀到 [`evals/skill-scenarios.json`](evals/skill-scenarios.json)；使用方法见 [`evals/README.md`](evals/README.md)。
- 本地链接检查发现一个原有 NPL 规范缺失图片；在文档索引注明，保留原规范字节。新增引用可解析。

本轮未进行真实 relay 登录、iPipe/业务构建测试或不同模型的对照实测。不能因此宣称换模型零退化；固定输入/输出合同、脚本门禁和可重复场景是今后测量退化的基础。后续经验只在匹配版本、触发条件和反例验证后进入参考材料，重复机械错误优先变成脚本或校验，避免继续累积无条件硬规则。
