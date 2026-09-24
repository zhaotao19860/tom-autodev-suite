"""Agent-facing adapter over the durable orchestrator and worker driver.

The bridge resolves user-facing card identifiers and assembles configured worker
adapters. Workflow decisions, validation, approval binding, and state transitions
remain owned by ``worker_driver`` and ``Orchestrator``.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import worker_driver
import run_brief
import workflow_spec

_RUN_ID = re.compile(r"^[0-9a-fA-F]{32}$")


def _json_safe(value: Any) -> Any:
    """Return a detached JSON-safe copy of a worker/bridge result."""
    import json

    return json.loads(json.dumps(value, ensure_ascii=False))


def resolve_run_target(orchestrator: Any, target: str) -> dict[str, Any]:
    """Resolve an explicit run id or an iCafe card id without guessing.

    Card lookup uses only the durable INTAKE identity in each run's event log.
    Multiple matches are returned as candidates so the caller can ask the user to
    choose one.
    """
    if not isinstance(target, str) or not target.strip():
        return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
    target = target.strip()
    if _RUN_ID.fullmatch(target):
        events = orchestrator.state.events(target)
        if not events:
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
        intake = events[0].get("payload") or {}
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": target,
            "state": events[-1].get("state"),
            "project": intake.get("project"),
            "requirement_id": intake.get("requirement_id"),
        }

    matches: list[dict[str, Any]] = []
    for latest in orchestrator.state.latest_states():
        run_id = latest.get("run_id")
        events = orchestrator.state.events(run_id) if isinstance(run_id, str) else []
        if not events:
            continue
        intake = events[0].get("payload") or {}
        if (
            intake.get("requirement_id") == target
            and events[-1].get("state") not in workflow_spec.terminal_states()
        ):
            matches.append({
                "run_id": run_id,
                "state": events[-1].get("state"),
                "project": intake.get("project"),
                "updated_at": events[-1].get("created_at"),
            })
    if not matches:
        return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
    if len(matches) > 1:
        return {
            "ok": False,
            "reason_code": "AMBIGUOUS_RUN",
            "target": target,
            "candidates": matches,
        }
    return {
        "ok": True,
        "reason_code": "OK",
        "target": target,
        **matches[0],
        "requirement_id": target,
    }


class AgentBridge:
    """Thin, stateless assembly boundary for agent-facing run operations."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        locks: Any,
        ipipe_api_factory: Callable[[str], Any] | None = None,
        icode_skill: str | None = None,
    ):
        self.orchestrator = orchestrator
        self.locks = locks
        self.ipipe_api_factory = ipipe_api_factory
        self.icode_skill = icode_skill

    def _worker_options(self, run_id: str) -> dict[str, Any]:
        options: dict[str, Any] = {"locks": self.locks}
        if self.ipipe_api_factory is not None:
            options["ipipe_api"] = self.ipipe_api_factory(run_id)
        if self.icode_skill is not None:
            options["icode_skill"] = self.icode_skill
        return options

    def drive(self, run_id: str) -> dict[str, Any]:
        result = worker_driver.advance(
            self.orchestrator, run_id, **self._worker_options(run_id)
        )
        return _json_safe(result)

    def continue_run(self, run_id: str) -> dict[str, Any]:
        pending_approval_handoffs = [
            handoff for handoff in self.orchestrator.state.incomplete_handoffs(run_id)
            if (handoff.get("payload") or {}).get("kind") == "APPROVAL_RESUME"
        ]
        options = self._worker_options(run_id)
        if pending_approval_handoffs:
            result = worker_driver.resume(self.orchestrator, run_id, **options)
        else:
            result = worker_driver.advance(self.orchestrator, run_id, **options)
        return _json_safe(result)

    def submit_draft(
        self, run_id: str, job_id: str, draft_content: dict[str, Any]
    ) -> dict[str, Any]:
        result = worker_driver.submit_draft(
            self.orchestrator, run_id, job_id, draft_content
        )
        return _json_safe(result)

    def status(self, target: str) -> dict[str, Any]:
        resolved = resolve_run_target(self.orchestrator, target)
        if not resolved.get("ok"):
            return _json_safe(resolved)
        return _json_safe(run_brief.build(self.orchestrator, resolved["run_id"]))

    def stop(self, target: str) -> dict[str, Any]:
        resolved = resolve_run_target(self.orchestrator, target)
        if not resolved.get("ok"):
            return _json_safe(resolved)
        return _json_safe(self.orchestrator.stop(resolved["run_id"]))
