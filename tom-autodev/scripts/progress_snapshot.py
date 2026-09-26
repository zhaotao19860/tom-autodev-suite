"""Build one read-only progress projection for a tom-autodev run.

The snapshot is deliberately a presentation model.  It may summarize durable
state, artifacts, and observations, but it never advances a run or authorizes an
external side effect.  Consumers such as ``status`` and notification renderers
should use this module instead of each inventing their own partial progress view.
"""
from __future__ import annotations

from typing import Any

import workflow_spec


_TERMINAL = frozenset({"RELEASE_SUCCESS", "STOPPED"})
_SUCCESS = frozenset({"SUCCESS", "SUCC", "PASS", "PASSED"})
_PHASE_ORDER = tuple(workflow_spec.STATES)
_PHASE_TITLES = {
    state: (spec.title or state)
    for state, spec in workflow_spec.STATES.items()
    if spec.title or state
}


def build(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """Return a stable, JSON-friendly progress snapshot for ``run_id``."""
    events = _events(orchestrator, run_id)
    if not events:
        return {
            "run_id": run_id,
            "current_phase": "RUN_NOT_FOUND",
            "overall": {"done": 0, "total": len(_PHASE_ORDER), "index": 0, "percent": 0},
            "phases": _phases("RUN_NOT_FOUND"),
            "tasks": _empty_tasks(),
            "repositories": [],
            "pipelines": {"status": "UNKNOWN", "modules": [], "done": 0, "total": 0},
            "release": {"status": "UNKNOWN", "done": 0, "total": 0, "waiting_for": []},
            "owner": {"name": "-", "kind": "unknown"},
            "next_action": {"owner": "-", "text": "运行不存在"},
        }

    state = str(events[-1].get("state") or "UNKNOWN")
    action = _next(orchestrator, run_id)
    brief = _safe_brief(orchestrator, run_id)
    phases = _phases(state)
    phase_index = next((index for index, item in enumerate(phases) if item["state"] == state), 0)
    phase_done = sum(item["status"] == "DONE" for item in phases)
    if state in _TERMINAL:
        phase_done = len(phases)
    tasks = _tasks(orchestrator, run_id, events, action)
    plan = _frozen_plan(orchestrator, run_id, events)
    monitoring = _monitoring(orchestrator, run_id)
    pipelines = _pipelines(orchestrator, run_id, plan, monitoring, events)
    release = _release(orchestrator, run_id, plan, pipelines, state)
    next_action = brief.get("next") if isinstance(brief, dict) else None
    next_action = next_action if isinstance(next_action, dict) else _next_action(action, state)
    owner = {
        "name": next_action.get("owner") or "-",
        "kind": "human" if next_action.get("owner") in {"你", "人工"} else "agent",
    }
    return {
        "run_id": run_id,
        "current_phase": state,
        "overall": {
            "done": phase_done,
            "total": len(phases),
            "index": phase_index,
            "percent": int(phase_done * 100 / len(phases)) if phases else 0,
        },
        "phases": phases,
        "tasks": tasks,
        "repositories": _repositories(orchestrator, run_id, plan, tasks, pipelines),
        "pipelines": pipelines,
        "release": release,
        "owner": owner,
        "next_action": {
            "owner": next_action.get("owner") or "-",
            "text": next_action.get("text") or "等待状态刷新",
        },
    }


def render(snapshot: dict[str, Any]) -> str:
    """Render the compact progress portion shared by status and notifications."""
    if not isinstance(snapshot, dict):
        return ""
    overall = snapshot.get("overall") or {}
    lines = [
        f"整体进度：{overall.get('done', 0)}/{overall.get('total', 0)} 阶段"
        f"（{overall.get('percent', 0)}%）",
        f"当前阶段：{snapshot.get('current_phase') or '-'}",
    ]
    tasks = snapshot.get("tasks") or {}
    if tasks.get("total"):
        lines.append(
            f"任务进度：{tasks.get('done', 0)}/{tasks.get('total', 0)} 完成"
            f" · 当前 {tasks.get('current') or '-'}"
        )
    repositories = snapshot.get("repositories") or []
    if repositories:
        lines.append("代码库：" + "、".join(
            f"{item.get('module') or '-'}={item.get('status') or 'UNKNOWN'}"
            for item in repositories
        ))
    pipelines = snapshot.get("pipelines") or {}
    if pipelines.get("total"):
        lines.append(
            f"流水线：{pipelines.get('done', 0)}/{pipelines.get('total', 0)} 通过"
            f" · {pipelines.get('status') or 'UNKNOWN'}"
        )
    release = snapshot.get("release") or {}
    if release.get("total"):
        lines.append(
            f"发布：{release.get('done', 0)}/{release.get('total', 0)} 模块具备证据"
            f" · {release.get('status') or 'PENDING'}"
        )
    next_action = snapshot.get("next_action") or {}
    lines.append(f"下一步（{next_action.get('owner') or '-'}）：{next_action.get('text') or '-'}")
    return "\n".join(lines)


def _events(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    reader = getattr(getattr(orchestrator, "state", None), "events", None)
    value = reader(run_id) if callable(reader) else []
    return value if isinstance(value, list) else []


def _next(orchestrator: Any, run_id: str) -> dict[str, Any]:
    try:
        from run_brief import _read_only_next
        value = _read_only_next(orchestrator, run_id)
    except Exception:  # noqa: BLE001 - status must remain best effort
        value = {}
    return value if isinstance(value, dict) else {}


def _safe_brief(orchestrator: Any, run_id: str) -> dict[str, Any]:
    # Avoid recursion: run_brief calls us, so only use its next projection here.
    return {}


def _next_action(action: dict[str, Any], state: str) -> dict[str, str]:
    if action.get("required_human_gate"):
        return {"owner": "你", "text": f"处理 {action['required_human_gate']} 审批"}
    if state in _TERMINAL:
        return {"owner": "-", "text": "运行已结束，无后续动作"}
    return {"owner": "Comate", "text": f"继续处理 {state}"}


def _phases(current: str) -> list[dict[str, Any]]:
    phases = []
    current_seen = False
    for state in _PHASE_ORDER:
        spec = workflow_spec.STATES[state]
        if spec.terminal:
            continue
        if state == current:
            status = "CURRENT"
            current_seen = True
        elif current in _TERMINAL or current_seen:
            status = "PENDING"
        else:
            status = "DONE"
        phases.append({
            "state": state,
            "title": _PHASE_TITLES.get(state, state),
            "status": status,
        })
    if current in _TERMINAL:
        phases.append({"state": current, "title": current, "status": "DONE"})
    elif current not in _PHASE_ORDER and current != "RUN_NOT_FOUND":
        phases.append({"state": current, "title": current, "status": "CURRENT"})
    return phases


def _empty_tasks() -> dict[str, Any]:
    return {"done": 0, "total": 0, "current": None, "status": "UNKNOWN", "items": []}


def _tasks(orchestrator: Any, run_id: str, events: list[dict[str, Any]], action: dict[str, Any]) -> dict[str, Any]:
    artifacts = getattr(orchestrator, "artifacts", None)
    latest = getattr(artifacts, "latest_phase", None)
    dag = latest(run_id, "TASKS", None) if callable(latest) else {}
    content = (dag.get("envelope") or {}).get("content") if isinstance(dag, dict) else {}
    nodes = content.get("nodes") if isinstance(content, dict) else []
    if not isinstance(nodes, list):
        nodes = []
    items = []
    protocol = None
    try:
        protocol = orchestrator.phase_protocol()
    except Exception:  # noqa: BLE001
        pass
    current = action.get("task_id")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        task_id = node.get("task_id") or node.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        reviewed = False
        if protocol is not None:
            try:
                reviewed = bool(protocol._task_reviewed(run_id, task_id))
            except Exception:  # noqa: BLE001
                reviewed = False
        status = "DONE" if reviewed else ("CURRENT" if task_id == current else "PENDING")
        items.append({
            "task_id": task_id,
            "module": node.get("business_module"),
            "status": status,
        })
    done = sum(item["status"] == "DONE" for item in items)
    return {
        "done": done,
        "total": len(items),
        "current": current if isinstance(current, str) else None,
        "status": "COMPLETE" if items and done == len(items) else ("ACTIVE" if items else "UNKNOWN"),
        "items": items,
    }


def _frozen_plan(orchestrator: Any, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        from pipeline_plan import frozen_plan
        value = frozen_plan(events)
        return value if isinstance(value, dict) else None
    except Exception:  # noqa: BLE001 - incomplete early runs have no plan
        return None


def _monitoring(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    reader = getattr(getattr(orchestrator, "state", None), "ipipe_monitoring", None)
    value = reader(run_id) if callable(reader) else []
    return value if isinstance(value, list) else []


def _pipelines(orchestrator: Any, run_id: str, plan: dict[str, Any] | None,
               monitoring: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    modules = list((plan or {}).get("required_modules") or [])
    if not modules:
        modules = sorted({
            str((item.get("checkpoint") or {}).get("module"))
            for item in monitoring
            if (item.get("checkpoint") or {}).get("module")
        })
    entries = []
    for module in modules:
        checkpoint = next(
            (item.get("checkpoint") or {} for item in reversed(monitoring)
             if (item.get("checkpoint") or {}).get("module") == module),
            {},
        )
        status = checkpoint.get("status") or "PENDING"
        entries.append({
            "module": module,
            "status": status,
            "build_id": checkpoint.get("build_id"),
            "stage_build_id": checkpoint.get("stage_build_id"),
        })
    done = sum(item["status"] in _SUCCESS for item in entries)
    failed = any(item["status"] in {"FAILURE", "FAILED"} for item in entries)
    status = "FAILURE" if failed else ("SUCCESS" if entries and done == len(entries) else ("MONITORING" if entries else "PENDING"))
    return {"status": status, "modules": entries, "done": done, "total": len(entries)}


def _release(orchestrator: Any, run_id: str, plan: dict[str, Any] | None,
             pipelines: dict[str, Any], state: str) -> dict[str, Any]:
    required = list((plan or {}).get("required_modules") or [])
    done = sum(item.get("status") in _SUCCESS for item in pipelines.get("modules") or [])
    if state == "RELEASE_SUCCESS":
        status = "SUCCESS"
        done = len(required) or done
    elif state == "RELEASE":
        status = "READY" if required and done == len(required) else "WAITING"
    elif state == "IPIPE":
        status = "WAITING"
    else:
        status = "PENDING"
    return {
        "status": status,
        "done": done,
        "total": len(required),
        "waiting_for": [module for module in required
                        if module not in {item.get("module") for item in pipelines.get("modules") or []
                                          if item.get("status") in _SUCCESS}],
    }


def _repositories(orchestrator: Any, run_id: str, plan: dict[str, Any] | None,
                  tasks: dict[str, Any], pipelines: dict[str, Any]) -> list[dict[str, Any]]:
    profile_reader = getattr(orchestrator, "_runtime_profile", None)
    profile = {}
    if callable(profile_reader):
        try:
            loaded = profile_reader(run_id)
            profile = loaded.get("profile") if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001
            profile = {}
    if not isinstance(profile, dict):
        profile = {}
    repositories = []
    configured = []
    configured.extend(profile.get("business_repos") or [])
    if isinstance(profile.get("test_repo"), dict):
        configured.append(profile["test_repo"])
    modules = {item.get("module"): item for item in pipelines.get("modules") or []}
    for repo in configured:
        if not isinstance(repo, dict) or not repo.get("module"):
            continue
        module = repo["module"]
        repositories.append({
            "module": module,
            "kind": "test" if repo is profile.get("test_repo") else "business",
            "branch": repo.get("branch"),
            "status": modules.get(module, {}).get("status") or
                      ("ACTIVE" if any(item.get("module") == module for item in tasks.get("items") or []) else "PENDING"),
        })
    if not repositories:
        repositories = [
            {"module": module, "kind": "unknown", "branch": None, "status": item.get("status")}
            for module, item in modules.items()
        ]
    return repositories
