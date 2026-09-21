# 发布记录

本 skill 属于 tom-autodev 套件，整套共 13 个，必须一起安装才能完成端到端流程。套件构成与各自职责见 SKILL.md 的依赖说明。

## 2026-09-21

- 折叠 `code-review-qa` 的语言无关审查方法论为纯 prompt references，使 tom-review 自足且更强，零新增运行时依赖（不复制任何脚本）。
- 新增 `references/review-heuristics.md`（数据流/边界/对抗/变体/恢复/一致性/契约七个阅读维度 + D-01..D-07 语义缺陷，逐条标注 Standards/Spec 轴）、`references/security-checklist.md`（OWASP Top 10 + 业务逻辑安全 + 攻击者可控输入置信度规则，喂 Standards 轴）、`references/severity-taxonomy.md`（红旗/黄旗量化阈值重表为 severity + blocking + classification）、`references/false-positive-suppression.md`（sink-first 误报防火墙，抑制→REJECTED_WITH_REASON、证据不足→NEEDS_CLARIFICATION，承接“宁可漏报不要误报”）、`references/rule-catalog.md`（G-SECRET/EXCEPT/INPUT/SEC/DB/PERF/LOG/ARCH、BIZ-* 规则 ID + 等级 + 阅读检查项）。
- SKILL.md 增补 “Review Method” 小节，说明五个 reference 的加载时机，并把 sink-first FP 纪律与 reject→REJECTED_WITH_REASON / 待确认→NEEDS_CLARIFICATION / confirmed→CONFIRMED 映射写入 Finding Reception；复用既有 `classification` 字段，未引入 HIGH/MEDIUM/BLOCK 判决词，保持 read-only / 无副作用框架不变。
- 丢弃 code-review-qa 的 knowbase/方舟/Bug-QA/iCafe/icode-fetch/upload 工作流、--no-upload/--no-push 逻辑、step 文件架构、报告模板生成与 PRD 拆解/检索流水线；未引入任何语言专属规则（保留在 tom-lang-* skills）。
- 无新增运行时依赖，纯 prompt 内容。

## 2026-09-07

- 首次发布到 OneTool 平台，skillId `28445`
- 发布范围由空间可见调整为广场可见
