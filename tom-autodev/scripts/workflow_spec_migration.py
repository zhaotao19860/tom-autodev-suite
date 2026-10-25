"""Move a legacy run to the current workflow policy under a dedicated approval.

The workflow policy is pinned at G0.  A later control-plane release must not silently
run a paused requirement under a different policy, but restarting the requirement would
discard valid phase artifacts and approvals.  This module provides the narrow middle
path: approve the old/new policy pin move, persist it as an auditable idempotency result,
and let the normal execution guard honour that move.

The migration intentionally does not rewrite phase events or approval rows.  The approval
material states that existing phase artifacts and hash-bound approvals remain in force;
the migration gate authorizes the policy version change itself.  A separate approval
delivery recovery can therefore reuse the original approval_id when its delivery outcome
is unknown.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import workflow_spec
from project_registry import load_profile


ACTION = "WORKFLOW_SPEC_MIGRATION"
_KEY_PREFIX = "workflow-spec-migration"


def _key(run_id: str, after: str | None = None) -> str:
    return (
        f"{_KEY_PREFIX}:{run_id}"
        if after is None
        else f"{_KEY_PREFIX}:{run_id}:after:{after}"
    )


def record_for(state: Any, run_id: str) -> dict[str, Any] | None:
    """Return the newest applied migration, following the append-only chain."""
    latest = None
    key = _key(run_id)
    seen: set[str] = set()
    for _ in range(16):
        current = state.idempotency_result(key)
        if not isinstance(current, dict):
            return latest
        latest = current
        new_hash = current.get("new_hash")
        if not isinstance(new_hash, str) or new_hash in seen:
            return latest
        seen.add(new_hash)
        key = _key(run_id, new_hash)
    return latest


def effective_pinned_hash(
    state: Any,
    run_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the pin after any approved migrations, or the original G0 pin.

    The chain is walked from its head, so a run migrated more than once reports the
    pin it actually reached. Starting from the newest record and comparing that
    record's `old_hash` with the G0 pin skipped every hop but the first, which left a
    twice-migrated run reported as drifted forever.
    """
    events = state.events(run_id) if events is None else events
    pinned = workflow_spec.pinned_spec_hash(events)
    seen: set[str] = set()
    migration = state.idempotency_result(_key(run_id))
    while isinstance(migration, dict):
        if migration.get("old_hash") != pinned:
            break
        reached = migration.get("new_hash")
        if not isinstance(reached, str) or reached in seen:
            break
        seen.add(reached)
        pinned = reached
        migration = state.idempotency_result(_key(run_id, reached))
    return pinned


def _input_hash(run_id: str, old_hash: str, new_hash: str) -> str:
    payload = {
        "run_id": run_id,
        "action": ACTION,
        "old_hash": old_hash,
        "new_hash": new_hash,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plan(orchestrator: Any, run_id: str) -> dict[str, Any]:
    events = orchestrator.state.events(run_id)
    if not events:
        return _failure("RUN_NOT_FOUND", run_id)
    old_hash = effective_pinned_hash(orchestrator.state, run_id)
    current_hash = workflow_spec.canonical_hash()
    if old_hash is None:
        return _failure("WORKFLOW_SPEC_PIN_MISSING", run_id)
    if old_hash == current_hash:
        return _failure(
            "NOTHING_TO_MIGRATE",
            run_id,
            old_hash=old_hash,
            new_hash=current_hash,
        )
    return {
        "ok": True,
        "reason_code": "OK",
        "run_id": run_id,
        "action": ACTION,
        "old_hash": old_hash,
        "new_hash": current_hash,
        "current_state": events[-1].get("state"),
        "preserves": [
            "phase artifacts",
            "existing approval_id/input_hash bindings",
            "external intent and receipt audit records",
        ],
        "input_hash": _input_hash(run_id, old_hash, current_hash),
    }


def _members(orchestrator: Any, run_id: str) -> tuple[list[str] | None, dict[str, Any] | None]:
    events = orchestrator.state.events(run_id)
    payload = events[0].get("payload") if events else None
    profile_path = payload.get("profile_path") if isinstance(payload, dict) else None
    if not isinstance(profile_path, str):
        return None, _failure("PROJECT_NOT_READY", run_id)
    loaded = load_profile(Path(profile_path))
    if not loaded.get("ready"):
        return None, {"ok": False, "run_id": run_id, **loaded}
    role_members = (loaded.get("profile") or {}).get("approval_channels", {}).get("role_members")
    if not isinstance(role_members, dict):
        return None, _failure("MEMBER_CONFIRMATION_REQUIRED", run_id)
    members = sorted({
        value
        for values in role_members.values()
        if isinstance(values, list)
        for value in values
        if isinstance(value, str) and value
    })
    if not members:
        return None, _failure("MEMBER_CONFIRMATION_REQUIRED", run_id)
    return members, None


def request(
    orchestrator: Any,
    run_id: str,
    *,
    comate_client: Any,
    infoflow_client: Any,
) -> dict[str, Any]:
    prepared = plan(orchestrator, run_id)
    if not prepared.get("ok"):
        return prepared
    members, error = _members(orchestrator, run_id)
    if error is not None:
        return error
    opened = orchestrator.request_infoflow_approval(
        run_id,
        ACTION,
        prepared["input_hash"],
        member_policy={"comate": members, "infoflow": members},
        evidence={
            "gate": {
                "subject": "将暂停中的运行迁移到当前 workflow policy",
                "effect": (
                    f"workflow_spec_hash {prepared['old_hash'][:12]}…"
                    f" → {prepared['new_hash'][:12]}…"
                ),
                "summary": [
                    "只迁移规则版本 pin",
                    "保留已有阶段 artifact、approval_id/input_hash 和外部审计记录",
                    "迁移后才允许恢复执行或处理未知审批投递",
                ],
                "references": [
                    {"label": "旧 workflow_spec_hash", "ref": prepared["old_hash"]},
                    {"label": "当前 workflow_spec_hash", "ref": prepared["new_hash"]},
                    {"label": "当前阶段", "ref": prepared["current_state"]},
                ],
            },
            "old_hash": prepared["old_hash"],
            "new_hash": prepared["new_hash"],
            "preserves": prepared["preserves"],
        },
        comate_client=comate_client,
        infoflow_client=infoflow_client,
        allow_workflow_spec_migration=True,
    )
    return {**prepared, "approval": opened}


def apply(orchestrator: Any, run_id: str, approval_id: str) -> dict[str, Any]:
    result = _apply(orchestrator, run_id, approval_id)
    if result.get("ok") and result.get("approval_id") == approval_id:
        # The explicit command consumed this approval; its resume handoff must not
        # linger as a second candidate for `continue`.
        orchestrator.state.complete_approval_handoffs(
            run_id, approval_id, actor="migrate-workflow-spec", reason="APPROVAL_CONSUMED"
        )
    return result


def _apply(orchestrator: Any, run_id: str, approval_id: str) -> dict[str, Any]:
    prepared = plan(orchestrator, run_id)
    if prepared.get("reason_code") == "NOTHING_TO_MIGRATE":
        return {"ok": True, "reason_code": "ALREADY_MIGRATED", **prepared}
    if not prepared.get("ok"):
        return prepared
    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict):
        return _failure("APPROVAL_NOT_FOUND", run_id)
    if (
        approval.get("run_id") != run_id
        or approval.get("action") != ACTION
        or approval.get("input_hash") != prepared["input_hash"]
    ):
        return _failure("APPROVAL_BINDING_MISMATCH", run_id, approval_id=approval_id)
    if approval.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED", run_id, approval_id=approval_id)

    # The next link hangs off the pin this run has actually reached, not off the
    # newest record in the chain: deriving it from the chain tail made a third
    # migration collide with the second one's key and fail as MIGRATION_CONFLICT.
    reached = prepared["old_hash"]
    key = _key(
        run_id,
        reached if reached != workflow_spec.pinned_spec_hash(orchestrator.state.events(run_id)) else None,
    )
    stored = {
        "run_id": run_id,
        "action": ACTION,
        "old_hash": prepared["old_hash"],
        "new_hash": prepared["new_hash"],
        "input_hash": prepared["input_hash"],
        "approval_id": approval_id,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    existing = orchestrator.state.idempotency_result(key)
    if existing is not None:
        if existing != stored and existing.get("new_hash") != stored["new_hash"]:
            return _failure("MIGRATION_CONFLICT", run_id)
        return {"ok": True, "reason_code": "ALREADY_MIGRATED", **existing}
    orchestrator.state.save_idempotency_result(key, stored)
    return {"ok": True, "reason_code": "OK", **stored}


def _failure(reason_code: str, run_id: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "run_id": run_id, **details}
