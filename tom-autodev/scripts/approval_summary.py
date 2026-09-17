"""What a gate is actually asking a person to approve.

An approval message that only carries an id and a deadline asks for a signature on
an unnamed thing: the approver cannot tell a requirement clarification from a
release, and the `input_hash` binding is worthless if nobody knows what it binds.
Every gate therefore states its subject, what approving it causes, and a summary
of the artifact derived from the very content the hash covers.
"""
from __future__ import annotations

import re
from typing import Any

import workflow_spec

# Subject and effect per gate, from the single workflow spec (workflow_spec.py).
_GATES: dict[str, tuple[str, str]] = workflow_spec.GATE_LABELS


def gate_intent(action: Any) -> dict[str, str]:
    subject, effect = _GATES.get(str(action), ("", ""))
    return {"subject": subject, "effect": effect}


def content_summary(content: Any) -> list[str]:
    """Summarize an artifact by its own shape, never by guessing at a schema.

    Dispatch is on the fields the schemas actually define, so an unrecognized
    artifact degrades to a field listing instead of inventing labels.
    """
    if not isinstance(content, dict):
        return []
    if "decision_result" in content:
        return _decision_log(content)
    if "behaviors" in content and "acceptance_scenarios" in content:
        return _spec(content)
    if "nodes" in content and "edges" in content:
        return _task_dag(content)
    if "plan_id" in content:
        return _task_plan(content)
    if _is_submit_descriptor(content):
        return _submit_descriptor(content)
    if "change_set_id" in content:
        return _change_set(content)
    return [f"字段 {name}：{_size(value)}" for name, value in sorted(content.items())][:8]


def _decision_log(content: dict[str, Any]) -> list[str]:
    lines = [
        f"结论 {_text(content.get('decision_result'))}，完成度 {_text(content.get('status'))}",
        f"决策 {_count(content.get('decisions'))} 条，"
        f"验收点 {_count(content.get('acceptance_delta'))} 条，"
        f"未决前沿 {_count(content.get('unresolved_frontier'))} 条",
    ]
    lines += _named(content.get("decisions"), ("decision_id", "id"), "question", "决策")
    lines += _named(content.get("acceptance_delta"), ("acceptance_id", "id"), "statement", "验收")
    return lines


def _spec(content: dict[str, Any]) -> list[str]:
    return [
        f"版本 {_text(content.get('version'))}",
        f"行为 {_count(content.get('behaviors'))} 条，"
        f"验收场景 {_count(content.get('acceptance_scenarios'))} 条，"
        f"非目标 {_count(content.get('non_goals'))} 条",
        f"测试接口 {_count(content.get('test_interface'))} 项，"
        f"环境要求 {_count(content.get('environment_requirements'))} 项",
    ]


_REPO_TOKEN = re.compile(r"([a-z0-9][a-z0-9._-]*/[a-z0-9._-]+/[a-z0-9._-]+)")


def _dag_node_repos(node: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Repositories a task touches, split into business and test sides.

    The task-dag has no dedicated repository field; each node names its repos in
    the leading token of every business/test change line (e.g. baidu/sysip/bgwagent).
    Branch and pinned revision are deliberately absent here — they are bound at
    WorkspaceGate and shown on the G4/G7 cards — so this only surfaces module names.
    """
    def repos(changes: Any) -> list[str]:
        seen: list[str] = []
        for line in changes if isinstance(changes, list) else []:
            if not isinstance(line, str):
                continue
            match = _REPO_TOKEN.match(line.strip())
            if match and match.group(1) not in seen:
                seen.append(match.group(1))
        return seen

    return repos(node.get("business_changes")), repos(node.get("test_changes"))


def _task_dag(content: dict[str, Any]) -> list[str]:
    lines = [
        f"任务 {_count(content.get('nodes'))} 个，依赖 {_count(content.get('edges'))} 条",
        f"验收点覆盖 {_count(content.get('acceptance_coverage'))} 项",
    ]
    nodes = content.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        business, test = _dag_node_repos(node)
        parts = []
        if business:
            parts.append(f"业务 {'、'.join(business)}")
        if test:
            parts.append(f"测试 {'、'.join(test)}")
        repo_text = "；".join(parts) if parts else "仓库未标注"
        lines.append(f"{_text(node.get('task_id'))} {repo_text}")
    lines.append("分支与 pinned 版本在 WORKSPACE 绑定后于 G4/G7 卡展示")
    return lines


def _task_plan(content: dict[str, Any]) -> list[str]:
    return [
        f"任务 {_text(content.get('task_id'))}",
        f"仓库 {_count(content.get('repositories'))} 个，"
        f"文件 {_count(content.get('files'))} 个，"
        f"测试 {_count(content.get('tests'))} 项",
        f"验收点 {_count(content.get('acceptance_point_ids'))} 项",
    ]


def _change_set(content: dict[str, Any]) -> list[str]:
    # The deviations are the part of a diff an approver cannot reconstruct from the
    # Task Plan they already approved, so they are named rather than only counted.
    lines = [
        f"任务 {_text(content.get('task_id'))}",
        f"测试用例 {_count(content.get('test_ids'))} 项，"
        f"追溯更新 {_count(content.get('traceability_delta'))} 项，"
        f"偏离计划 {_count(content.get('deviations'))} 处",
        f"全量 diff 哈希 {_short(content.get('full_diff_hash'))}",
    ]
    return lines + _named(content.get("deviations"), (), "reason", "偏离")


def _is_submit_descriptor(content: dict[str, Any]) -> bool:
    """Distinguish the G7 submission descriptor from the G5 change-set artifact."""
    return all(
        isinstance(content.get(key), str) and content.get(key)
        for key in ("module", "target_branch", "commit_revision")
    ) and isinstance(content.get("revision_set"), dict)


def _submit_descriptor(content: dict[str, Any]) -> list[str]:
    """Show the identity an approver is authorizing iCode to submit."""
    revision_set = content.get("revision_set") or {}
    rows = []
    for role in ("business", "test"):
        repository = revision_set.get(role)
        if not isinstance(repository, dict):
            continue
        rows.append(
            f"{role} 仓库 {_text(repository.get('module'))}，"
            f"分支 {_text(repository.get('branch'))}，"
            f"版本 {_short(repository.get('revision'))}"
        )
    if content.get("change_number") or content.get("existing_cr"):
        mode = "追加现有 CR"
    elif content.get("submission_mode") == "create_new_cr":
        mode = "新建 CR"
    else:
        mode = "提交前由 iCode 校验新建/追加"
    return [
        f"提交方式 {mode}",
        f"主仓库 {_text(content.get('module'))}，分支 {_text(content.get('target_branch'))}",
        f"提交版本 {_short(content.get('commit_revision'))}",
        f"变更集 {_text(content.get('change_set_id'))}，版本集 {_short(content.get('revision_set_id'))}",
        *rows,
    ]


def _named(
    value: Any, id_keys: tuple[str, ...], text_key: str, label: str, limit: int = 4
) -> list[str]:
    if not isinstance(value, list):
        return []
    lines = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        identifier = next(
            (str(item[key]) for key in id_keys if isinstance(item.get(key), str)), ""
        )
        text = item.get(text_key)
        text = str(text) if isinstance(text, str) else ""
        head = " ".join(part for part in (label, identifier) if part)
        lines.append(f"{head}：{_clip(text)}" if text else head)
    remaining = len(value) - len(lines)
    if remaining > 0:
        lines.append(f"其余 {remaining} 条见产物文档")
    return lines


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def _size(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return f"{len(value)} 项"
    return _clip(str(value))


def _text(value: Any) -> str:
    return _clip(str(value)) if isinstance(value, (str, int, float)) else "-"


def _short(value: Any) -> str:
    return f"{value[:12]}…" if isinstance(value, str) and len(value) > 12 else _text(value)


def _clip(value: str, limit: int = 60) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
