from __future__ import annotations

import hashlib
import re
import json
from html import unescape
from html.parser import HTMLParser
from typing import Any

"""Human-readable knowledge-base rendering of a phase artifact.

The canonical JSON is what the control plane hashes, but a run's collaboration
directory is read by people: reviewers, QA and the requirement owner. So each
document leads with a rendered view and keeps the canonical JSON in a fenced
appendix, which stays byte-exact for auditing. Rendering must be deterministic —
the published markdown feeds the KU idempotency key.

One field breaks that arrangement: a change set carries whole unified diffs. A
document that inlines them twice — once rendered, once in the appendix — is both
unreadable and large enough that the knowledge base has silently dropped the body.
The patches are already stored content-addressed in the artifact store, so the
document summarizes them (file list, added and removed line counts, `full_diff_hash`)
and the appendix elides the bytes behind their own sha256. Everything else stays
byte-exact.
"""

_SNAPSHOT_LABELS = (
    ("canonical_card_id", "卡片"),
    ("title", "标题"),
    ("type", "类型"),
    ("status", "状态"),
    ("content_hash", "内容哈希"),
)
_DECISION_LABELS = (("decision_result", "结论"), ("status", "完成度"))
_CHANGE_SET_LABELS = (
    ("change_set_id", "改动集"),
    ("task_id", "任务"),
    ("full_diff_hash", "全量 diff 哈希"),
    ("candidate_hash", "候选哈希"),
)
# Spec sections that are plain string lists, in the order a reviewer reads them.
_SPEC_LISTS = (
    ("boundaries", "边界"),
    ("errors", "错误与降级"),
    ("compatibility", "兼容性"),
    ("test_interface", "测试接口"),
    ("environment_requirements", "环境要求"),
    ("non_goals", "非目标"),
    ("risks", "风险"),
    ("rollback", "回滚"),
    ("release_evidence", "发布证据"),
)
# A behavior description usually opens with its own short subject, e.g.
# "URL 归一化：按 §4.1 的 17 条规则…". Promoting that subject into the heading is what
# makes fourteen behaviors scannable; anything longer is left in the body.
_SUBJECT_MAX = 32
# A string longer than this is a payload, not a value: it is summarized in the
# rendered view and elided from the appendix behind its own sha256.
_INLINE_MAX = 600

_FIELD_LABELS = {
    "acceptance_point_id": "验收点",
    "assertions": "断言",
    "axis": "评审维度",
    "blocking": "是否阻塞",
    "business": "业务",
    "capability_slice": "能力切片",
    "complete": "是否完整",
    "completeness_state": "完整性",
    "conclusion": "结论",
    "decision_result": "决策结果",
    "description": "说明",
    "disposition_reason": "处置理由",
    "evidence_refs": "证据引用",
    "finding_ids": "发现编号",
    "findings": "评审发现",
    "fixture": "夹具",
    "from": "前置任务",
    "from_plan": "原计划",
    "id": "编号",
    "identity": "身份",
    "item": "检查项",
    "job_ids": "作业编号",
    "kind": "类型",
    "module": "模块",
    "module_releases": "模块发布项",
    "name": "名称",
    "order": "顺序",
    "path": "路径",
    "pipeline_plan_hash": "流水线计划哈希",
    "reason": "原因",
    "release_id": "发布 ID",
    "remaining_risks": "剩余风险",
    "result": "执行结果",
    "revision": "修订",
    "revisions": "修订",
    "role": "仓库角色",
    "severity": "严重程度",
    "spec": "规格符合性",
    "standards": "标准符合性",
    "summary": "摘要",
    "test_id": "测试用例",
    "title": "标题",
    "to": "后续任务",
    "unresolved_frontier": "未决前沿",
    "acceptance_point_ids": "验收点",
    "acceptance_coverage": "验收覆盖",
    "acceptance_delta": "验收增量",
    "acceptance_scenarios": "验收场景",
    "as_implemented": "实际实现",
    "attempt": "尝试次数",
    "axes": "评审维度",
    "baseline_revisions": "基线修订",
    "behaviors": "行为",
    "business_changes": "业务改动",
    "business_module": "业务模块",
    "build_id": "构建",
    "change_set_hash": "改动集哈希",
    "checklist": "检查清单",
    "classification": "分类",
    "comparison": "对比结果",
    "completion_predicate": "完成判定",
    "deviations": "计划偏离",
    "edges": "依赖关系",
    "environment_fingerprint": "环境指纹",
    "evidence": "证据",
    "evidence_state": "证据状态",
    "failure_signature": "失败签名",
    "files": "文件",
    "fixtures": "夹具",
    "frozen_revisions": "冻结修订",
    "g4_input_hash": "G4 输入哈希",
    "given": "前置条件",
    "hypothesis": "根因假设",
    "interfaces": "接口",
    "ipipe_parameters": "iPipe 参数",
    "ipipe_stages": "iPipe 阶段",
    "jobs": "作业",
    "job_id": "作业 ID",
    "location": "位置",
    "log_evidence": "日志证据",
    "minimal_verification": "最小验证",
    "modules": "模块",
    "nodes": "任务节点",
    "options": "选项",
    "owner": "负责人",
    "pipeline_id": "流水线",
    "plan_id": "计划 ID",
    "provider": "评审提供方",
    "repair_direction": "修复方向",
    "repair_plan": "修复计划",
    "repair_scope": "修复范围",
    "repositories": "代码仓库",
    "reproduction": "复现",
    "release_evidence": "发布证据",
    "release_rule": "发布规则",
    "remote_evidence_refs": "远程证据引用",
    "repair_diff_hash": "修复 diff 哈希",
    "risks": "风险",
    "rollback": "回滚",
    "route": "后续路线",
    "scenario_ids": "验收场景",
    "source_refs": "来源引用",
    "stage_id": "阶段 ID",
    "stages": "阶段",
    "status": "状态",
    "symbols": "符号",
    "task_ids": "任务",
    "task_id": "任务",
    "test_changes": "测试改动",
    "test_ids": "测试用例",
    "tests": "测试",
    "then": "预期结果",
    "traceability": "追溯",
    "verdict": "评审结论",
    "when": "触发动作",
}

# This is deliberately kept in the renderer instead of in the individual child
# skills.  The KU page is the hand-off surface for the whole team, so every
# phase must answer the same questions even when its schema is different.
_PHASE_GUIDE = {
    "INTAKE": {
        "name": "需求快照与运行章程",
        "purpose": "固定需求、项目、仓库基线、参与人和本次运行的协作边界。",
        "upstream": "iCafe 卡片、项目 profile、知识库父目录和参与人信息。",
        "downstream": "GRILL 使用这份快照核对澄清范围，并据此准备 G0。",
        "handoff": "需求负责人确认快照中的项目、仓库/分支、人员、流水线和协作群信息。",
    },
    "GRILL": {
        "name": "需求澄清与决策日志",
        "purpose": "把不明确的需求前沿变成用户可选择、可追溯的决策和验收增量。",
        "upstream": "00 需求快照、卡片正文、项目知识源和结构化问题。",
        "downstream": "SPEC 将按决策材料、术语和验收点编写行为规格。",
        "handoff": "确认每个选择的理由、验收增量和未决项；G1 只审批决策日志，不替用户答题。",
    },
    "SPEC": {
        "name": "行为规格说明",
        "purpose": "定义目标行为、边界、异常、兼容性和可执行的验收场景。",
        "upstream": "01 决策日志、现状分析、代码/知识库证据和验收增量。",
        "downstream": "TASKS 按行为和验收覆盖拆分可并行、可验证的任务。",
        "handoff": "规格负责人确认行为、Given/When/Then、测试接口、环境要求和非目标。",
    },
    "TASKS": {
        "name": "任务拆解与验收覆盖",
        "purpose": "把规格拆成有依赖关系、责任边界和验收覆盖的任务 DAG。",
        "upstream": "02 行为规格说明及其验收点追溯。",
        "downstream": "WORKSPACE 绑定每个任务的仓库、分支、基线和工作区。",
        "handoff": "确认任务节点、依赖、并行关系、负责人与每个验收点的覆盖。",
    },
    "WORKSPACE": {
        "name": "工作区与基线绑定",
        "purpose": "记录任务实际使用的仓库、分支、revision、worktree、锁和基线来源。",
        "upstream": "03 任务 DAG、profile 和项目 skill 的仓库信息。",
        "downstream": "PLAN 在这个不可漂移的基线上指定文件、符号和测试入口。",
        "handoff": "确认业务/测试仓库、分支、revision、工作区状态和 iPipe 绑定。",
    },
    "PLAN": {
        "name": "任务实现计划",
        "purpose": "把一个任务变成可执行的文件级、接口级和测试级实现计划。",
        "upstream": "04 工作区基线、03 任务节点、02 规格和项目 skill 约束。",
        "downstream": "IMPLEMENT 按计划生成业务和产品测试候选改动。",
        "handoff": "确认修改文件/符号、接口、测试用例、iPipe 参数、风险和回滚。",
    },
    "IMPLEMENT": {
        "name": "候选改动说明",
        "purpose": "解释实际代码和测试改动解决了什么、改变了什么，以及与计划的偏离。",
        "upstream": "05 任务实现计划、工作区基线和代码生成结果。",
        "downstream": "REVIEW 对候选改动执行 Standards 与 Spec 双轴评审。",
        "handoff": "确认文件级变更、行为前后差异、测试覆盖、完整 diff 引用和偏离原因。",
    },
    "REVIEW": {
        "name": "变更评审报告",
        "purpose": "给出基于固定 diff 的独立质量判断，并区分已核实、已驳回和待澄清问题。",
        "upstream": "06 候选改动、02 规格、05 计划和评审基线。",
        "downstream": "通过后进入 SUBMIT；阻塞问题进入 DIAGNOSE，不直接修改代码。",
        "handoff": "确认 verdict、每条 finding 的分类和理由，以及提交或诊断路线。",
    },
    "SUBMIT": {
        "name": "iCode 提交与流水线计划",
        "purpose": "固定 CR、revision、目标分支和要触发的 iPipe 配置。",
        "upstream": "07 评审结果、候选改动和 profile 中的提交/流水线规则。",
        "downstream": "IPIPE 使用这份固定提交描述采集远程编译、测试和回归证据。",
        "handoff": "确认提交对象、仓库 revision、目标分支、流水线和允许参数。",
    },
    "IPIPE": {
        "name": "远程验证报告",
        "purpose": "记录真实 iPipe build、stage、job、环境、结果和可复核的远程证据。",
        "upstream": "08 iCode 提交与流水线计划及其固定 revision。",
        "downstream": "通过后进入 RELEASE；失败则带着 failure signature 进入 DIAGNOSE。",
        "handoff": "确认 build/stage/job、结果、日志/证据链接、版本、失败原因和是否需要 G8。",
    },
    "DIAGNOSE": {
        "name": "失败诊断与修复决策",
        "purpose": "从复现和对比证据中给出一个可验证根因、最小验证和修复路线。",
        "upstream": "07 评审或 09 远程验证中的失败证据、日志、复现和版本信息。",
        "downstream": "按 route 回到 SPEC、PLAN 或架构复核；修复后重新走对应验证。",
        "handoff": "确认现象、证据、排除项、单一假设、最小验证和修复范围。",
    },
    "RELEASE": {
        "name": "发布验证证书",
        "purpose": "证明获批 revision 在目标环境完成发布动作并通过发布后验证。",
        "upstream": "09 远程验证报告、G9 审批材料、release profile 和 revision map。",
        "downstream": "发布证据及门禁通过后才可进入 RELEASE_SUCCESS；剩余风险和未闭环项需继续追踪。",
        "handoff": "确认发布版本、revision、环境、release receipt、验证结果和剩余风险。",
    },
}


def phase_guide(phase: str | None) -> dict[str, str]:
    guide = _PHASE_GUIDE.get(str(phase or "").upper())
    return dict(guide) if guide else {
        "name": str(phase or "阶段文档"),
        "purpose": "记录本阶段实际产出，供后续阶段和协作者接手。",
        "upstream": "当前运行提供的上游 artifact 和证据。",
        "downstream": "由工作流状态机决定的下一阶段。",
        "handoff": "核对本文档正文、产物哈希和审计附录后再继续。",
    }


def render_phase_markdown(
    title: str,
    content: Any,
    content_hash: str,
    canonical: str,
    *,
    phase: str | None = None,
    task_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    resolved_phase = phase or _phase_from_title(title)
    guide = phase_guide(resolved_phase)
    audit_context = context if isinstance(context, dict) else {}
    current_task = task_id or audit_context.get("task_id") or _value(content, "task_id")
    lines = [
        f"# {title}",
        "",
        f"> 阶段：{guide['name']}",
        "> 文档状态：渲染预览；审批、持久化和推进状态不由本文档推断",
        f"> 当前任务：{current_task if current_task not in (None, '') else '未提供'}",
        "",
        "## 一句话结论",
        "",
        _one_line_conclusion(content, guide),
        "",
        "## 本阶段要解决什么",
        "",
        guide["purpose"],
        "",
        "## 上游输入",
        "",
        f"- {guide['upstream']}",
    ]
    references = _upstream_references(content)
    if references:
        lines += [f"- {item}" for item in references]
    lines += ["", "## 实际内容", "", *content_lines(content, resolved_phase)]
    lines += [
        "",
        "## 当前可核对内容",
        "",
        *_observable_items(content, guide),
        "",
        "## 产出物",
        "",
        f"- 本阶段规范化产物哈希：`{content_hash}`",
        f"- 下游读取的 canonical artifact：见文末“规范化 JSON”附录（{len(canonical)} 字符）。",
        "",
        "## 与下阶段的关系",
        "",
        f"- 本阶段输入：{guide['upstream']}",
        f"- 下一阶段使用：{guide['downstream']}",
        "",
        "## 风险、缺口与待办",
        "",
        *_risk_items(content),
        "",
        "## 交接清单",
        "",
        f"- {guide['handoff']}",
        "- 先阅读正文中的结论、实际内容、可核对内容和待办，再依据审计附录核对 hash、revision、审批和远程证据。",
        "",
        "## 审计与证据",
        "",
        "- 正文仅展示输入材料；审批结果、存储状态和远程执行结果需独立核验，不能以渲染成功代替。",
    ]
    return "\n".join(
        lines + _audit_context(audit_context) + _appendix(content_hash, canonical)
    ).rstrip()


def content_lines(content: Any, phase: str | None = None) -> list[str]:
    """The readable body of one phase artifact, without the document frame."""
    if isinstance(content, dict) and "canonical_card_id" in content:
        return _requirement_snapshot(content)
    if isinstance(content, dict) and "decision_result" in content:
        return _decision_log(content)
    if isinstance(content, dict) and "behaviors" in content and "acceptance_scenarios" in content:
        return _spec(content)
    if isinstance(content, dict) and "business_patch" in content and "test_patch" in content:
        return _change_set(content)
    if isinstance(content, dict) and _looks_like_phase_schema(phase, content):
        return _schema_content(phase, content)
    if isinstance(content, dict):
        return _generic(content)
    return _structured_lines(content)


def canonical_appendix(canonical: str) -> str:
    """The appendix body a document must carry for this canonical JSON.

    A pure function of the canonical bytes, so the publisher can require the page to
    carry exactly this and still be sure the page and the artifact cannot drift. For
    everything but a change set it *is* the canonical JSON; where a field is payload
    sized, the value is replaced by its own length and sha256, which keeps the elided
    bytes verifiable against the copy in the artifact store.
    """
    abridged, elided = _abridged_canonical(canonical)
    return canonical if not elided else _canonical_json(abridged)


def _appendix(content_hash: str, canonical: str) -> list[str]:
    _, elided = _abridged_canonical(canonical)
    if not elided:
        note = (
            f"产物哈希 `{content_hash}`。下面是被哈希的规范化 JSON；知识库在渲染时可能还原 HTML 实体，"
            "以产物哈希为准。"
        )
    else:
        note = (
            f"产物哈希 `{content_hash}`。下面的规范化 JSON 已省略过长字段，"
            "被哈希的是未省略的原文，按产物哈希在产物库中取用："
            + "；".join(
                f"`{path}` {length} 字符，sha256 `{digest}`" for path, length, digest in elided
            )
            + "。"
        )
    return ["", "## 附录：规范化 JSON", "", note, "", "```json", canonical_appendix(canonical), "```"]


def _abridged_canonical(canonical: str) -> tuple[Any, list[tuple[str, int, str]]]:
    """Abridge the canonical JSON, or leave it alone if it is not a JSON container."""
    try:
        parsed = json.loads(canonical)
    except (TypeError, ValueError):
        return canonical, []
    if not isinstance(parsed, (dict, list)):
        return canonical, []
    return _abridge(parsed)


def _requirement_snapshot(content: dict[str, Any]) -> list[str]:
    lines = ["## 需求快照", ""]
    lines += _labelled(content, _SNAPSHOT_LABELS)
    people = content.get("responsible_people")
    if isinstance(people, list) and people:
        names = "、".join(
            f"{person.get('name', '')}({person.get('username', '')})"
            for person in people
            if isinstance(person, dict)
        )
        lines.append(f"- 负责人：{names}")
    for key, label in (("created", "创建"), ("modified", "最近修改")):
        stamp = content.get(key)
        if isinstance(stamp, dict):
            user = stamp.get("user") if isinstance(stamp.get("user"), dict) else {}
            lines.append(f"- {label}：{stamp.get('time', '')} {user.get('name', '')}")
    fields = content.get("fields")
    if isinstance(fields, dict) and fields:
        lines += ["", "### 卡片字段", ""]
        lines += _structured_lines(fields)
    acceptance = content.get("acceptance")
    lines += ["", "### 卡片自带验收点", ""]
    lines += (
        _structured_lines(acceptance)
        if isinstance(acceptance, list) and acceptance
        else ["未提供验收点；需在 GRILL 阶段核对验收标准。"]
    )
    body = content.get("body")
    html = content.get("html")
    readable_body = _html_to_text(body) if isinstance(body, str) else ""
    readable_html = _html_to_text(html) if isinstance(html, str) else ""
    lines += ["", "### 卡片正文", ""]
    if readable_body:
        lines.append(readable_body)
    if readable_html and readable_html != readable_body:
        if readable_body:
            lines += ["", "### HTML 正文补充", ""]
        lines.append(readable_html)
    if not readable_body and not readable_html:
        lines.append("未提供可读正文。")
    lines.append("")
    lines.append("原始 body/html 仍保留在规范化 JSON 附录中，本文档不据此推断审批或持久化状态。")
    for key, label in (("attachments", "附件"), ("links", "关联链接")):
        if content.get(key):
            lines += ["", f"### {label}", ""] + _structured_lines(content[key])
    return lines


def _decision_log(content: dict[str, Any]) -> list[str]:
    lines = ["## 澄清结论", ""]
    lines += _labelled(content, _DECISION_LABELS)
    lines += ["", "## 决策", ""]
    for decision in content.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        lines += [
            f"### {decision.get('id', '')} {decision.get('question', '')}",
            "",
            f"- 选择：{decision.get('choice', '')}",
            "- 备选：" + "；".join(_plain(item) for item in decision.get("options") or []),
            f"- 理由：{decision.get('rationale', '')}",
            "- 证据：" + "；".join(_plain(item) for item in decision.get("evidence") or []),
            "",
        ]
    questions = content.get("choice_questions")
    if isinstance(questions, list) and questions:
        lines += ["## Comate 待选择题", ""]
        for question in questions:
            if not isinstance(question, dict):
                continue
            lines += [
                f"### {question.get('id', '')} {question.get('title', '')}",
                "",
                f"- 题目：{question.get('prompt', '')}",
                f"- 选项：" + "；".join(
                    f"{option.get('id', '')} {option.get('label', '')}"
                    for option in question.get("options") or []
                    if isinstance(option, dict)
                ),
                f"- 影响：{question.get('impact', '')}",
                "",
            ]
    points = content.get("acceptance_delta")
    if isinstance(points, list) and points:
        lines += ["## 验收点", ""]
        for point in points:
            if isinstance(point, dict):
                lines.append(
                    f"- `{point.get('id', '')}` {point.get('statement', '')}"
                    f"（确认人 {point.get('decided_by', '')}）"
                )
        lines.append("")
    frontier = content.get("unresolved_frontier")
    lines += ["## 未决前沿", ""]
    lines += (
        [f"- {_plain(item)}" for item in frontier]
        if isinstance(frontier, list) and frontier
        else ["未决前沿列表为空；不代表整体无阻塞。" if frontier == []
              else "未决前沿未知；不能据此判断无阻塞项。"]
    )
    glossary = content.get("glossary_delta")
    if isinstance(glossary, dict) and glossary:
        lines += ["", "## 术语", ""]
        lines += _structured_lines(glossary)
    candidates = content.get("adr_candidates")
    if isinstance(candidates, list) and candidates:
        lines += ["", "## ADR 候选", ""]
        lines += [f"- {_plain(item)}" for item in candidates]
    evidence = content.get("source_evidence")
    if isinstance(evidence, list) and evidence:
        lines += ["", "## 来源证据", ""]
        lines += [f"- {_plain(item)}" for item in evidence]
    return lines


def _spec(content: dict[str, Any]) -> list[str]:
    """Render a Spec the way it is reviewed: behaviors, then how each is judged.

    The generic renderer prints a list of dicts as one JSON line per item, which
    makes a Spec unreadable exactly where it matters — a reviewer has to read every
    behavior and every Given/When/Then before approving G2.
    """
    lines = ["## 概览", ""]
    version = content.get("version")
    if isinstance(version, str) and version:
        lines.append(f"- Spec 版本：{version}")
    lines += [
        f"- 行为 {_count(content.get('behaviors'))} 条，"
        f"验收场景 {_count(content.get('acceptance_scenarios'))} 条，"
        f"验收点追溯 {_count(content.get('traceability'))} 条",
        "",
        "## 行为",
        "",
    ]
    for behavior in content.get("behaviors") or []:
        if not isinstance(behavior, dict):
            continue
        subject, body = _split_subject(str(behavior.get("description", "")))
        lines += [f"### {behavior.get('id', '')} {subject}".rstrip(), "", body, ""]
    lines += ["## 验收场景", ""]
    for scenario in content.get("acceptance_scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        points = scenario.get("acceptance_point_ids")
        lines += [
            f"### {scenario.get('id', '')}",
            "",
            "- 验收点：" + "、".join(_plain(item) for item in points or []),
            f"- Given：{scenario.get('given', '')}",
            f"- When：{scenario.get('when', '')}",
            f"- Then：{scenario.get('then', '')}",
            "",
        ]
    for key, label in _SPEC_LISTS:
        values = content.get(key)
        if isinstance(values, list) and values:
            lines += [f"## {label}", ""]
            lines += [f"- {_plain(item)}" for item in values]
            lines.append("")
    traces = content.get("traceability")
    if isinstance(traces, list) and traces:
        lines += ["## 追溯", "", "| 验收点 | 行为 | 验收场景 |", "| --- | --- | --- |"]
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            lines.append(
                f"| {trace.get('acceptance_point_id', '')} "
                f"| {'、'.join(_plain(item) for item in trace.get('behavior_ids') or [])} "
                f"| {'、'.join(_plain(item) for item in trace.get('scenario_ids') or [])} |"
            )
    return lines


def _change_set(content: dict[str, Any]) -> list[str]:
    """Render a change set as a patch summary, never as the patch itself."""
    lines = ["## 概览", ""]
    lines += _labelled(content, _CHANGE_SET_LABELS)
    for key, label in (("baseline_revisions", "基线"), ("revisions", "产出")):
        revisions = content.get(key)
        if isinstance(revisions, dict):
            lines.append(
                f"- {label}修订：业务 `{revisions.get('business', '')}`，"
                f"测试 `{revisions.get('tests', '')}`"
            )
    for key, label in (("business_patch", "业务补丁"), ("test_patch", "测试补丁")):
        patch = content.get(key)
        if not isinstance(patch, str):
            continue
        files = _diff_files(patch)
        added = sum(item[1] for item in files)
        removed = sum(item[2] for item in files)
        lines += [
            "",
            f"## {label}",
            "",
            f"- {len(files)} 个文件，新增 {added} 行，删除 {removed} 行，共 {len(patch)} 字符",
            f"- 全文以 `full_diff_hash` 定位，存储状态需核验，不在本文档内联；`{key}` 的 sha256 为 "
            f"`{_digest(patch)}`",
            "",
        ]
        lines += [f"- `{path}` +{plus} -{minus}" for path, plus, minus in files]
    tests = content.get("test_ids")
    if isinstance(tests, list) and tests:
        lines += ["", "## 测试", ""]
        lines += [f"- `{_plain(item)}`" for item in tests]
    traces = content.get("traceability_delta")
    if isinstance(traces, list) and traces:
        lines += ["", "## 追溯增量", "", "| 验收点 | 测试 |", "| --- | --- |"]
        for trace in traces:
            if isinstance(trace, dict):
                lines.append(
                    f"| {trace.get('acceptance_point_id', '')} "
                    f"| {'、'.join(_plain(item) for item in trace.get('test_ids') or [])} |"
                )
    deviations = content.get("deviations")
    lines += ["", "## 与计划的偏离", ""]
    if isinstance(deviations, list) and deviations:
        for deviation in deviations:
            if isinstance(deviation, dict):
                lines += [
                    f"- 计划：{deviation.get('from_plan', '')}",
                    f"  - 实现：{deviation.get('as_implemented', '')}",
                    f"  - 理由：{deviation.get('reason', '')}",
                ]
    else:
        lines.append("偏离列表为空；不代表计划一致性或审批已经核验。"
                     if deviations == [] else "计划偏离信息未知，需核对实现与计划。")
    return lines


def _looks_like_phase_schema(phase: str | None, content: dict[str, Any]) -> bool:
    """Only use a phase renderer when the payload has that phase's shape.

    Small hand-written fixtures and legacy artifacts are intentionally left to the
    generic renderer.  This also keeps phase inference from turning an arbitrary
    ``{"nodes": ...}`` value into a misleading task DAG.
    """
    required = {
        "TASKS": ("nodes", "edges", "acceptance_coverage"),
        "PLAN": ("plan_id", "repositories", "files", "tests"),
        "REVIEW": ("axes", "findings", "verdict", "provider"),
        "DIAGNOSE": ("frozen_revisions", "reproduction", "route", "repair_plan"),
        "IPIPE": ("pipeline_id", "build_id", "stages", "jobs", "remote_evidence_refs"),
        "RELEASE": ("pipeline_id", "release_id", "release_evidence", "remote_evidence_refs"),
    }.get(str(phase or "").upper())
    return bool(required and all(key in content for key in required))


def _schema_content(phase: str | None, content: dict[str, Any]) -> list[str]:
    renderers = {
        "TASKS": _tasks,
        "PLAN": _plan,
        "REVIEW": _review,
        "DIAGNOSE": _diagnose,
        "IPIPE": _ipipe,
        "RELEASE": _release,
    }
    renderer = renderers.get(str(phase or "").upper())
    return renderer(content) if renderer else _generic(content)


def _tasks(content: dict[str, Any]) -> list[str]:
    lines = [
        "## 概览",
        "",
        f"- 任务节点：{_count(content.get('nodes'))} 个",
        f"- 依赖关系：{_count(content.get('edges'))} 条",
        f"- 验收覆盖：{_count(content.get('acceptance_coverage'))} 条",
        "",
        "## 任务节点",
        "",
    ]
    for node in content.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        lines += [f"### {node.get('task_id', '')} {node.get('title', '')}".rstrip(), ""]
        for key in (
            "capability_slice", "business_module", "business_changes", "test_changes",
            "test_ids", "fixtures", "ipipe_stages", "completion_predicate",
            "acceptance_point_ids",
        ):
            if node.get(key) not in (None, "", [], {}):
                lines += _field_lines(key, node[key])
        lines.append("")
    lines += ["## 依赖关系", ""]
    edges = content.get("edges") or []
    if edges:
        lines += [
            f"- `{edge.get('from', '')}` → `{edge.get('to', '')}`"
            for edge in edges if isinstance(edge, dict)
        ]
    else:
        lines.append("未提供依赖关系。")
    lines += ["", "## 验收覆盖", ""]
    for item in content.get("acceptance_coverage") or []:
        if isinstance(item, dict):
            lines.append(
                f"- `{item.get('acceptance_point_id', '')}`："
                f"{_join_values(item.get('task_ids'))}"
            )
    return lines


def _plan(content: dict[str, Any]) -> list[str]:
    lines = ["## 概览", ""]
    for key in ("plan_id", "task_id", "g4_input_hash"):
        if content.get(key) not in (None, ""):
            lines += _field_lines(key, content[key])
    lines += ["", "## 代码仓库", ""]
    for repository in content.get("repositories") or []:
        if isinstance(repository, dict):
            lines.append(
                f"- {repository.get('role', '')}：{repository.get('path', '')}"
                f"（revision `{repository.get('revision', '')}`）"
            )
    for key in ("files", "modules", "symbols", "interfaces", "fixtures", "risks", "rollback",
                "acceptance_point_ids"):
        if content.get(key) not in (None, "", [], {}):
            lines += ["", f"## {_label_for(key)}", ""]
            lines += _value_lines(content[key])
    lines += ["", "## 测试计划", ""]
    for test in content.get("tests") or []:
        if not isinstance(test, dict):
            continue
        lines += [
            f"### {test.get('test_id', '')}",
            "",
            f"- 夹具：{test.get('fixture', '')}",
            "- 断言：",
        ]
        lines += [f"  - {_plain(item)}" for item in test.get("assertions") or []]
    if content.get("ipipe_parameters") not in (None, {}, ""):
        lines += ["", "## iPipe 参数", ""]
        lines += _structured_lines(content["ipipe_parameters"], 0)
    lines += ["", "## 检查清单", ""]
    for item in content.get("checklist") or []:
        if isinstance(item, dict):
            lines.append(f"- {item.get('order', '')}. {item.get('item', '')}")
    return lines


def _review(content: dict[str, Any]) -> list[str]:
    lines = ["## 评审结果", ""]
    for key in ("task_id", "change_set_hash", "verdict", "completeness_state"):
        if content.get(key) not in (None, ""):
            lines += _field_lines(key, content[key])
    baseline = content.get("baseline_revisions")
    if isinstance(baseline, dict):
        lines += ["- 基线修订：", f"  - 业务：{baseline.get('business', '')}",
                  f"  - 测试：{baseline.get('tests', '')}"]
    provider = content.get("provider")
    if isinstance(provider, dict):
        lines += [f"- 评审提供方：{provider.get('kind', '')}（{provider.get('identity', '')}）"]
    lines += ["", "## 评审维度", ""]
    axes = content.get("axes")
    if isinstance(axes, dict):
        for name in ("standards", "spec"):
            axis = axes.get(name)
            if isinstance(axis, dict):
                lines += [
                    f"- {name}：完整性={_status_value(axis.get('complete'))}，"
                    f"发现 {_count(axis.get('finding_ids'))} 条"
                ]
    lines += ["", "## 评审发现", ""]
    findings = content.get("findings") or []
    if not findings:
        lines.append("未提供评审发现；不能据此判断没有问题。")
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        lines += [
            f"### {finding.get('id', '')} [{finding.get('severity', '未知')}] "
            f"{finding.get('location', '')}".rstrip(),
            "",
            f"- 维度：{finding.get('axis', '')}",
            f"- 证据：{finding.get('evidence', '')}",
            f"- 验收点：{_join_values(finding.get('acceptance_point_ids'))}",
            f"- 阻塞：{_status_value(finding.get('blocking'))}",
            f"- 分类：{finding.get('classification', '未知')}",
        ]
        if finding.get("disposition_reason") not in (None, ""):
            lines.append(f"- 处置理由：{finding['disposition_reason']}")
        lines.append("")
    return lines


def _diagnose(content: dict[str, Any]) -> list[str]:
    lines = ["## 诊断概览", ""]
    for key in (
        "task_id", "attempt", "build_id", "stage_id", "job_id",
        "environment_fingerprint", "classification", "route",
        "failure_signature", "evidence_state", "repair_scope",
        "repair_diff_hash",
    ):
        if content.get(key) not in (None, ""):
            lines += _field_lines(key, content[key])
    revisions = content.get("frozen_revisions")
    if isinstance(revisions, dict):
        lines += ["- 冻结修订：", f"  - 业务：{revisions.get('business', '')}",
                  f"  - 测试：{revisions.get('tests', '')}"]
    for key in (
        "log_evidence", "reproduction", "comparison", "minimal_verification",
        "repair_plan",
    ):
        if content.get(key) not in (None, "", [], {}):
            lines += ["", f"## {_label_for(key)}", ""]
            lines += _value_lines(content[key])
    for key in ("hypothesis", "repair_direction"):
        if content.get(key) not in (None, ""):
            lines += ["", f"## {_label_for(key)}", "", f"- {_plain(content[key])}"]
    return lines


def _ipipe(content: dict[str, Any]) -> list[str]:
    lines = ["## 远程验证概览", ""]
    for key in (
        "pipeline_id", "build_id", "module", "environment_fingerprint",
        "status", "classification", "failure_signature", "release_rule",
    ):
        if content.get(key) not in (None, ""):
            lines += _field_lines(key, content[key])
    revisions = content.get("revisions")
    if isinstance(revisions, dict):
        lines += ["- 产出修订：", f"  - 业务：{revisions.get('business', '')}",
                  f"  - 测试：{revisions.get('tests', '')}"]
    lines += ["", "## 阶段", ""]
    for stage in content.get("stages") or []:
        if isinstance(stage, dict):
            lines.append(
                f"- `{stage.get('stage_id', '')}`：{stage.get('status', '未知')}，"
                f"作业 {_join_values(stage.get('job_ids'))}"
            )
    lines += ["", "## 作业", ""]
    for job in content.get("jobs") or []:
        if isinstance(job, dict):
            lines.append(
                f"- `{job.get('job_id', '')}`：{job.get('status', '未知')}，"
                f"证据 {_join_values(job.get('evidence_refs'))}"
            )
    for key in ("release_evidence", "remote_evidence_refs"):
        lines += ["", f"## {_label_for(key)}", ""]
        lines += _value_lines(content.get(key) or ["未提供。"])
    return lines


def _release(content: dict[str, Any]) -> list[str]:
    lines = ["## 发布概览", ""]
    for key in (
        "pipeline_id", "build_id", "release_id", "module",
        "environment_fingerprint", "status", "release_rule",
        "pipeline_plan_hash",
    ):
        if content.get(key) not in (None, ""):
            lines += _field_lines(key, content[key])
    revisions = content.get("revisions")
    if isinstance(revisions, dict):
        lines += ["- 发布修订：", f"  - 业务：{revisions.get('business', '')}",
                  f"  - 测试：{revisions.get('tests', '')}"]
    for key in ("release_evidence", "remote_evidence_refs"):
        lines += ["", f"## {_label_for(key)}", ""]
        lines += _value_lines(content.get(key) or ["未提供。"])
    modules = content.get("module_releases")
    if isinstance(modules, dict) and modules:
        lines += ["", "## 模块发布项", ""]
        for name in sorted(modules):
            lines.append(f"### {name}")
            lines += _structured_lines(modules[name], 0)
    return lines


def _field_lines(key: str, value: Any) -> list[str]:
    label = _label_for(key)
    if isinstance(value, (dict, list)):
        return [f"- {label}："] + _structured_lines(value, 1)
    return [f"- {label}：{_plain(value)}"]


def _value_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [f"- {_plain(item)}" for item in value]
    if isinstance(value, dict):
        return _structured_lines(value, 0)
    return [f"- {_plain(value)}"]


def _structured_lines(value: Any, depth: int = 0) -> list[str]:
    """Render nested data as bullets, never as a JSON object."""
    indent = "  " * depth
    if isinstance(value, dict):
        lines: list[str] = []
        for key in sorted(value):
            child = value[key]
            label = _label_for(key)
            if isinstance(child, (dict, list)):
                lines.append(f"{indent}- {label}：")
                lines += _structured_lines(child, depth + 1)
            else:
                lines.append(f"{indent}- {label}：{_plain(child)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}-")
                lines += _structured_lines(item, depth + 1)
            else:
                lines.append(f"{indent}- {_plain(item)}")
        return lines
    return [f"{indent}- {_plain(value)}"]


def _label_for(key: Any) -> str:
    text = str(key)
    return _FIELD_LABELS.get(text, text.replace("_", " "))


def _join_values(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(_plain(item) for item in value)
    if value in (None, ""):
        return "未提供"
    return _plain(value)


def _status_value(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知" if value in (None, "") else _plain(value)


def _diff_files(patch: str) -> list[tuple[str, int, int]]:
    """Per-file added and removed line counts read off a unified diff."""
    files: list[tuple[str, int, int]] = []
    path, added, removed = "", 0, 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            if path:
                files.append((path, added, removed))
            path, added, removed = line.rsplit(" b/", 1)[-1], 0, 0
        elif line.startswith(("+++", "---", "@@")):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    if path:
        files.append((path, added, removed))
    return files


def _abridge(value: Any, path: str = "") -> tuple[Any, list[tuple[str, int, str]]]:
    """Replace payload-sized strings with a marker, reporting what was elided."""
    if isinstance(value, str) and len(value) > _INLINE_MAX:
        digest = _digest(value)
        return f"<省略 {len(value)} 字符，sha256 {digest}>", [(path or "(root)", len(value), digest)]
    if isinstance(value, dict):
        result, elided = {}, []
        for key in sorted(value):
            child, child_elided = _abridge(value[key], f"{path}.{key}" if path else key)
            result[key] = child
            elided += child_elided
        return result, elided
    if isinstance(value, list):
        items, elided = [], []
        for index, item in enumerate(value):
            child, child_elided = _abridge(item, f"{path}[{index}]")
            items.append(child)
            elided += child_elided
        return items, elided
    return value, []


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _split_subject(description: str) -> tuple[str, str]:
    head, separator, rest = description.partition("：")
    if separator and rest and len(head) <= _SUBJECT_MAX and not any(mark in head for mark in "。；，"):
        return head, rest.strip()
    return "", description


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _phase_from_title(title: str) -> str | None:
    # Numbers changed when WORKSPACE and SUBMIT gained directory entries.
    # Match only the phase-name segment, never a task/build suffix or a number.
    name = str(title or "").split("/", 1)[0].strip().lower()
    name = re.sub(r"^\d{2}[\s._-]*", "", name).replace("_", "-")
    aliases = {
        "INTAKE": ("intake", "requirement-snapshot", "需求快照", "运行章程"),
        "GRILL": ("grill", "decision-log", "需求澄清", "决策日志"),
        "SPEC": ("spec", "行为规格"),
        "TASKS": ("tasks", "task-dag", "任务拆解"),
        "WORKSPACE": ("workspace", "工作区", "基线绑定"),
        "PLAN": ("plan", "task-plan", "任务实现计划", "实现计划"),
        "IMPLEMENT": ("implement", "change-set", "候选改动"),
        "REVIEW": ("review", "变更评审", "评审报告"),
        "SUBMIT": ("submit", "icode", "提交与流水线"),
        "IPIPE": ("ipipe", "远程验证"),
        "DIAGNOSE": ("diagnose", "diagnosis", "失败诊断"),
        "RELEASE": ("release", "发布验证", "发布证据"),
    }
    for phase, names in aliases.items():
        for alias in names:
            if re.search(r"[a-z]", alias):
                if re.match(re.escape(alias) + r"(?:$|[-\s（(])", name):
                    return phase
            elif alias in name:
                return phase
    return None


def _value(content: Any, key: str) -> Any:
    return content.get(key) if isinstance(content, dict) else None


def _one_line_conclusion(content: Any, guide: dict[str, str]) -> str:
    if isinstance(content, dict):
        for key in (
            "summary",
            "conclusion",
            "decision_result",
            "verdict",
            "result",
            "status",
            "failure_class",
        ):
            value = content.get(key)
            if value not in (None, "", [], {}):
                return f"材料中的{_label_for(key)}：{_plain(value)}（不代表审批或门禁结论）"
        for key in ("title", "description", "purpose"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0]
    if isinstance(content, list) and content:
        return f"本次材料包含 {len(content)} 项内容，结论及审批状态待核对。"
    return f"本文展示“{guide['name']}”的输入材料；未提供明确结论，状态未知。"


def _upstream_references(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return []
    values: list[str] = []
    for key, label in (
        ("parent_artifact_hash", "上游 artifact"),
        ("input_hash", "输入哈希"),
        ("spec_version", "Spec 版本"),
        ("source_revisions", "源 revision"),
        ("baseline_revisions", "基线 revision"),
        ("build_id", "build"),
        ("failure_signature", "失败签名"),
    ):
        value = content.get(key)
        if value not in (None, "", [], {}):
            values.append(f"{label}：`{_summarized(value)}`")
    return values


def _observable_items(content: Any, guide: dict[str, str]) -> list[str]:
    if not isinstance(content, dict):
        return ["- 仅展示所提供的内容；确认、审批及存储状态未知。"]
    candidates: list[str] = []
    for key, label in (
        ("decisions", "决策材料"),
        ("acceptance_delta", "验收点"),
        ("acceptance_scenarios", "验收场景"),
        ("behaviors", "行为"),
        ("nodes", "任务节点"),
        ("findings", "评审发现"),
        ("test_ids", "测试引用"),
        ("remote_evidence_refs", "远程证据引用"),
        ("reproduction", "复现材料"),
        ("hypothesis", "诊断假设"),
        ("release_version", "发布版本"),
    ):
        value = content.get(key)
        if isinstance(value, list) and value:
            candidates.append(f"- {label}：{len(value)} 项。")
        elif isinstance(value, str) and value:
            candidates.append(f"- {label}：{_summarized(value)}")
        elif isinstance(value, dict) and value:
            candidates += _field_lines(key, value)
    if candidates:
        return candidates
    for key, label in (
        ("status", "阶段状态"),
        ("verdict", "评审结论"),
        ("decision_result", "决策结果"),
        ("result", "执行结果"),
    ):
        value = content.get(key)
        if value not in (None, "", [], {}):
            return [f"- {label}：{_summarized(value)}"]
    return [f"- 可核对“{guide['name']}”正文和产物哈希；审批及存储状态未知。"]


def _risk_items(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return ["- 风险和阻塞状态未知；未提供可核验的风险材料。"]
    risks: list[str] = []
    for key, label in (
        ("risks", "风险"),
        ("unresolved_frontier", "未决前沿"),
        ("open_items", "未闭环项"),
        ("pending_items", "待办"),
        ("deviations", "与计划的偏离"),
        ("remaining_risks", "剩余风险"),
        ("failures", "失败项"),
    ):
        value = content.get(key)
        if isinstance(value, list) and value:
            risks.append(f"- {label}：{len(value)} 项，详见实际内容。")
        elif isinstance(value, str) and value:
            risks.append(f"- {label}：{_summarized(value)}")
        elif isinstance(value, dict) and value:
            risks += _field_lines(key, value)
    return risks or ["- 风险和阻塞状态未知；缺失或空列表不能证明无阻塞，需核对审批与验证证据。"]


def _generic(content: dict[str, Any], depth: int = 2) -> list[str]:
    lines: list[str] = []
    for key, value in sorted(content.items()):
        if isinstance(value, dict) and value:
            lines += ["", f"{'#' * depth} {_label_for(key)}", ""]
            lines += _generic(value, min(depth + 1, 6))
        elif isinstance(value, list) and value:
            lines += ["", f"{'#' * depth} {_label_for(key)}", ""]
            lines += _structured_lines(value)
        else:
            lines.append(f"- {_label_for(key)}：{_display_value(key, value)}")
    return lines or ["未提供实际内容。"]


def _summarized(value: Any) -> str:
    """A payload-sized value is named by its length and digest, not printed."""
    plain = _plain(value)
    if len(plain) <= _INLINE_MAX:
        return plain
    return f"{plain[:_INLINE_MAX]}…（共 {len(plain)} 字符，sha256 `{_digest(plain)}`）"


def _labelled(content: dict[str, Any], labels: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        f"- {label}：{_plain(content[key])}" for key, label in labels if content.get(key) is not None
    ]


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return "未知（unknown）" if value.lower() == "unknown" else value
    if value is None:
        return "未知（未提供）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return "；".join(f"{_label_for(k)}：{_plain(v)}" for k, v in sorted(value.items())) or "空记录"
    if isinstance(value, list):
        return "；".join(_plain(item) for item in value) or "空列表"
    return str(value)


def _display_value(key: str, value: Any) -> str:
    if key in ("business_patch", "test_patch") and isinstance(value, str):
        return f"补丁不内联，共 {len(value)} 字符，sha256 `{_digest(value)}`"
    if key in ("body", "html") and isinstance(value, str):
        return _html_to_text(value)
    if key in ("log", "stdout", "stderr"):
        return _summarized(value)
    return _plain(value)


def _audit_context(context: dict[str, Any]) -> list[str]:
    """Caller-supplied provenance, not an approval verifier or a clock."""
    if not context:
        return ["- 未提供审计 context；不推断运行、审批、负责人或更新时间。"]
    labels = (
        ("run_id", "运行 ID"), ("task_id", "任务 ID"), ("owner", "负责人"),
        ("updated_at", "更新时间"), ("input_hash", "输入哈希"),
        ("approval_id", "审批引用"), ("approval_status", "审批状态（调用方提供）"),
        ("source_refs", "来源引用"), ("source_evidence", "来源证据"),
        ("source_revisions", "来源修订"),
    )
    lines = ["- 以下审计上下文由调用方提供；审批引用不等于审批通过，时间不由渲染器生成。"]
    for key, label in labels:
        value = context.get(key)
        if isinstance(value, (dict, list)) and value:
            lines += [f"- {label}："] + _structured_lines(value, 1)
        else:
            lines.append(f"- {label}：{_plain(value) if value not in ('', [], {}) else '未提供'}")
    other = {key: value for key, value in context.items() if key not in dict(labels)}
    if other:
        lines += ["- 其他审计上下文："] + _structured_lines(other, 1)
    return lines


class _ReadableHTML(HTMLParser):
    """Keep paragraph/list/table boundaries and link targets; omit executable text."""

    _blocks = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
               "ul", "ol", "li", "table", "tr", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden.append(tag)
        if self.hidden:
            return
        attributes = dict(attrs)
        if tag in self._blocks or tag in ("br", "hr"):
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "a":
            self.links.append(attributes.get("href") or "")
        elif tag == "img":
            self.parts.append(attributes.get("alt") or "")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self._blocks:
            self.parts.append("\n")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href.startswith(("https://", "http://", "/#", "#")):
                self.parts.append(f"（{href}）")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    # Some card adapters return entity-escaped HTML, others plain text.
    if not re.search(r"<[a-zA-Z][^>]*>", value) and re.search(r"&lt;/?[a-zA-Z]", value):
        value = unescape(value)
    parser = _ReadableHTML()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    return "\n".join(
        line for line in (re.sub(r"[ \t\r\f\v\u00a0]+", " ", part).strip()
                          for part in text.splitlines()) if line
    )
