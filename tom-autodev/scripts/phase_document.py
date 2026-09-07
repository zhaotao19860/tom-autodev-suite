from __future__ import annotations

import json
from typing import Any

"""Human-readable knowledge-base rendering of a phase artifact.

The canonical JSON is what the control plane hashes, but a run's collaboration
directory is read by people: reviewers, QA and the requirement owner. So each
document leads with a rendered view and keeps the canonical JSON in a fenced
appendix, which stays byte-exact for auditing. Rendering must be deterministic —
the published markdown feeds the KU idempotency key.
"""

# Fields whose value is raw card HTML: unreadable inline and already carried by
# the appendix, so the rendered view links to the card instead.
_OPAQUE = ("body", "html")
_SNAPSHOT_LABELS = (
    ("canonical_card_id", "卡片"),
    ("title", "标题"),
    ("type", "类型"),
    ("status", "状态"),
    ("content_hash", "内容哈希"),
)
_DECISION_LABELS = (("decision_result", "结论"), ("status", "完成度"))
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


def render_phase_markdown(title: str, content: Any, content_hash: str, canonical: str) -> str:
    lines = [f"# {title}", ""]
    if isinstance(content, dict) and "canonical_card_id" in content:
        lines += _requirement_snapshot(content)
    elif isinstance(content, dict) and "decision_result" in content:
        lines += _decision_log(content)
    elif isinstance(content, dict) and "behaviors" in content and "acceptance_scenarios" in content:
        lines += _spec(content)
    elif isinstance(content, dict):
        lines += _generic(content)
    else:
        lines += [f"`{_plain(content)}`"]
    lines += [
        "",
        "## 附录：规范化 JSON",
        "",
        f"产物哈希 `{content_hash}`。下面是被哈希的规范化 JSON；知识库在渲染时可能还原 HTML 实体，"
        "以产物哈希为准。",
        "",
        "```json",
        canonical,
        "```",
    ]
    return "\n".join(lines).rstrip()


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
        lines += [f"- {key}：{value}" for key, value in sorted(fields.items())]
    acceptance = content.get("acceptance")
    lines += ["", "### 卡片自带验收点", ""]
    lines += (
        [f"- {_plain(item)}" for item in acceptance]
        if isinstance(acceptance, list) and acceptance
        else ["卡片没有验收点，验收标准由 GRILL 阶段与需求负责人确认后记入决策日志。"]
    )
    lines += ["", "卡片正文为不可变快照，原文见附录 JSON 的 `body` 与 `html` 字段。"]
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
        else ["无阻塞项。"]
    )
    glossary = content.get("glossary_delta")
    if isinstance(glossary, dict) and glossary:
        lines += ["", "## 术语", ""]
        lines += [f"- {key}：{value}" for key, value in sorted(glossary.items())]
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


def _split_subject(description: str) -> tuple[str, str]:
    head, separator, rest = description.partition("：")
    if separator and rest and len(head) <= _SUBJECT_MAX and not any(mark in head for mark in "。；，"):
        return head, rest.strip()
    return "", description


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _generic(content: dict[str, Any], depth: int = 2) -> list[str]:
    lines: list[str] = []
    for key, value in sorted(content.items()):
        if key in _OPAQUE:
            continue
        if isinstance(value, dict) and value:
            lines += ["", f"{'#' * depth} {key}", ""]
            lines += _generic(value, min(depth + 1, 6))
        elif isinstance(value, list) and value:
            lines += ["", f"{'#' * depth} {key}", ""]
            lines += [f"- {_plain(item)}" for item in value]
        elif not isinstance(value, (dict, list)):
            lines.append(f"- {key}：{_plain(value)}")
    return lines


def _labelled(content: dict[str, Any], labels: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        f"- {label}：{_plain(content[key])}" for key, label in labels if content.get(key) is not None
    ]


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
