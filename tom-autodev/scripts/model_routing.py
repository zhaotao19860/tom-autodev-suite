"""Which model should fill the current ProducerJob (advisory, agent-neutral).

The controller does not run models; the driving agent fills a ProducerJob. This module
only answers "which model is cheap enough and strong enough for this job", by mapping
the job to a `tom-model-router` role/tier and asking that skill's router. When the
router skill is not installed or fails, the advice is simply absent: routing never
blocks, changes or validates a run.

Mapping:
  GRILL / SPEC / TASKS / PLAN / DIAGNOSE -> design
  IMPLEMENT                              -> implement; repair when re-entered after a
                                            diagnosis or after an earlier fill of the task
  REVIEW                                 -> review, avoiding the IMPLEMENT model's family
  tier: hotfix/express -> S, standard -> M, full -> L
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import agent_host
import workflow_spec

ROUTER_SKILL = "tom-model-router"
_DESIGN_PHASES = frozenset({"GRILL", "SPEC", "TASKS", "PLAN", "DIAGNOSE"})
_CLASS_TIER = {"hotfix": "S", "express": "S", "standard": "M", "full": "L"}
_TIMEOUT_SECONDS = 30


def _receipts(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    getter = getattr(getattr(orchestrator, "state", None), "model_execution_receipts", None)
    if not callable(getter):
        return []
    try:
        rows = getter(run_id)
    except Exception:  # noqa: BLE001 - advice must not fail the caller
        return []
    return [row for row in rows if isinstance(row, dict)]


def job_route(orchestrator: Any, run_id: str, action: dict[str, Any]) -> dict[str, Any] | None:
    """The router role/tier/attempt for a skill action, or None for a non-skill action."""
    phase = action.get("phase")
    if not action.get("child_skill") or not phase:
        return None
    events = orchestrator.state.events(run_id)
    tier = _CLASS_TIER.get(workflow_spec.change_class_of(events), "M")
    receipts = _receipts(orchestrator, run_id)
    task_id = action.get("task_id")
    implemented = [r for r in receipts if r.get("phase") == "IMPLEMENT"
                   and (task_id is None or r.get("task_id") in (None, task_id))]
    route: dict[str, Any] = {"phase": phase, "tier": tier, "attempt": 1}
    if phase in _DESIGN_PHASES:
        route["role"] = "design"
    elif phase == "IMPLEMENT":
        reentered = isinstance(action.get("reentry"), dict)
        route["role"] = "repair" if reentered or implemented else "implement"
        route["attempt"] = 1 + len([r for r in implemented if r.get("model")])
    elif phase == "REVIEW":
        route["role"] = "review"
        impl_model = next((r.get("model") for r in reversed(implemented) if r.get("model")), None)
        if impl_model:
            route["impl_model"] = impl_model
    else:
        return None
    return route


def _runners() -> str:
    # Only Claude Code can dispatch to its `tmr-*` subagents; others go through a CLI.
    return "subagent,codex,comate,claude" if agent_host.active_agent() == "claude" \
        else "codex,comate,claude"


def advise(orchestrator: Any, run_id: str, action: dict[str, Any] | None) -> dict[str, Any] | None:
    """`tom-model-router pick` for this action, or None when there is nothing to advise."""
    if not isinstance(action, dict):
        return None
    route = job_route(orchestrator, run_id, action)
    if route is None:
        return None
    skill = agent_host.find_skill(ROUTER_SKILL)
    script = skill / "router.py" if skill is not None else None
    if script is None or not script.is_file():
        return {**route, "available": False, "reason": f"{ROUTER_SKILL} 未安装"}
    argv = [sys.executable, str(script), "pick", "--role", route["role"], "--tier", route["tier"],
            "--attempt", str(route["attempt"]), "--runners", _runners()]
    if route.get("impl_model"):
        argv += ["--impl-model", route["impl_model"]]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
                              stdin=subprocess.DEVNULL)
        picked = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        picked = None
    if not isinstance(picked, dict):
        return {**route, "available": False, "reason": "router 调用失败"}
    return {**route, "available": True, "pick": picked}
