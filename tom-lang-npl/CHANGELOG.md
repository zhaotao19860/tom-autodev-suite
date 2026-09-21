# 发布记录

本 skill 属于 tom-autodev 套件，整套共 12 个。端到端流程依赖套件内控制面、相位与所选项目/语言规则；职责见 SKILL.md，外部参考 skill 不再是运行时依赖。

## 2026-09-21

- 合并 `npl-coder` 的 NPL **语言**知识，使本 skill 对 NPL 语言问题自包含。
- 新增 `references/npl-docs/`：逐字复制三份权威 NPL 语言文档（`NPL_Specification.1.5.1.md`、`NPL_Coding_Guidelines_Baidu.md`、`NPL_Compilation_Error_Fixup_Examples_Baidu.md`）。
- 新增 `references/npl-idioms.md`：NPL 生成惯用法（特性门控 wrapper、强度仲裁、芯片条件编译、flex-editor 分区逆序、bus 模型、Critical Constraints 表）；xflow 专有 bus 名已弱化为模式或标注为项目示例。
- 新增 `references/npl-compile-diagnostics.md`：nlc 前端 / xfc 后端两段编译模型、6 条 pitfall、`add_header` tap-point 规则、build.sh 假成功陷阱；与 `error-patterns.md`（路由分类）互补。
- 扩充 `references/npl-core-rules.md`：补入无 `return`（用 `if` 包裹）与顶层函数需 `@NPL_PRAGMA` mapping 两条硬规则及容量/唯一性/对齐约束（已对既有 field-validity、C/C++ 类比条目去重）。
- `SKILL.md` 与 `error-patterns.md` 仅增加指针行，不搬正文。
- 本地 build/simulate 命令、项目拓扑、芯片 PDF、运维经验等刻意留在 `tom-project-xflow`，未纳入本语言适配 skill。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28448`
- 发布范围由空间可见调整为广场可见
