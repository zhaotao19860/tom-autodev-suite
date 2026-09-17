from __future__ import annotations

import hashlib
import json
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


def render_phase_markdown(title: str, content: Any, content_hash: str, canonical: str) -> str:
    lines = [f"# {title}", ""]
    if isinstance(content, dict) and "canonical_card_id" in content:
        lines += _requirement_snapshot(content)
    elif isinstance(content, dict) and "decision_result" in content:
        lines += _decision_log(content)
    elif isinstance(content, dict) and "behaviors" in content and "acceptance_scenarios" in content:
        lines += _spec(content)
    elif isinstance(content, dict) and "business_patch" in content and "test_patch" in content:
        lines += _change_set(content)
    elif isinstance(content, dict):
        lines += _generic(content)
    else:
        lines += [f"`{_plain(content)}`"]
    return "\n".join(lines + _appendix(content_hash, canonical)).rstrip()


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
            f"- 全文按 `full_diff_hash` 存于产物库，不在本文档内联；`{key}` 的 sha256 为 "
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
        lines.append("无偏离，改动与获批的任务计划一致。")
    return lines


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
            lines.append(f"- {key}：{_summarized(value)}")
    return lines


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
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
