"""Refresh one read-only Infoflow bubble per run from the durable progress projection."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from collaboration import group_id_for_run
from execution_guard import execution_guard
from progress_snapshot import build as build_progress, phase_route


_OPERATION = "infoflow.work-card.upsert"
_NOT_DISPATCHED = frozenset({
    "INFOFLOW_GATEWAY_UNAVAILABLE",
    "INFOFLOW_GATEWAY_MISSING",
    "INFOFLOW_GATEWAY_INSTALL_FAILED",
    "INFOFLOW_GATEWAY_JOURNAL_MISMATCH",
    "MESSAGE_CONTENT_INVALID",
    "NOTIFY_RECIPIENTS_INVALID",
})


def refresh(orchestrator: Any, client: Any, run_id: str) -> dict[str, Any]:
    state = orchestrator.state
    blocked = execution_guard(state, run_id)
    if blocked is not None:
        return blocked
    group_id = group_id_for_run(state, run_id)
    if group_id is None or not callable(getattr(client, "send_work_card", None)):
        return {"ok": True, "reason_code": "WORK_CARD_UNAVAILABLE", "run_id": run_id}
    try:
        snapshot = build_progress(orchestrator, run_id)
        payload = _card_payload(snapshot, group_id)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "reason_code": "WORK_CARD_PROJECTION_FAILED",
                "run_id": run_id, "detail": str(error)}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    pending = [item for item in state.pending_intents(run_id) if item.get("operation") == _OPERATION]
    if pending:
        return {"ok": False, "reason_code": "WORK_CARD_QUERY_REQUIRED", "run_id": run_id,
                "intent_id": pending[0]["intent_id"]}
    attempts = [item for item in state.external_results(run_id)
                if item.get("intent", {}).get("operation") == _OPERATION]
    completed = [item for item in attempts if _successful_receipt(item)]
    last = completed[-1].get("intent", {}).get("payload", {}) if completed else {}
    if last.get("content_hash") == digest and last.get("target_id") == group_id:
        return {"ok": True, "reason_code": "WORK_CARD_UNCHANGED", "run_id": run_id}
    # Sequence distinguishes returning to an earlier state from a no-op. A repeated
    # card render always targets the same remote bubble, never an additional message.
    key = f"infoflow.work-card:{run_id}:{group_id}:{len(attempts)}"
    intent_payload = {
        "run_id": run_id, "target_id": group_id, "content_hash": digest,
    }
    claim = state.claim_intent(run_id, _OPERATION, key, intent_payload)
    if claim["status"] == "CONFLICT":
        return {"ok": False, "reason_code": "WORK_CARD_CONFLICT", "run_id": run_id}
    if claim["status"] == "EXISTING":
        # A render may have succeeded before the receipt was persisted. Re-sending would
        # be an unverified external write; leave the intent for platform reconciliation.
        existing = state.result_by_idempotency_key(key)
        if isinstance(existing, dict) and _successful_receipt(existing):
            return {"ok": True, "reason_code": "WORK_CARD_UNCHANGED", "run_id": run_id}
        return {"ok": False, "reason_code": "WORK_CARD_QUERY_REQUIRED", "run_id": run_id,
                "intent_id": claim["intent"]["intent_id"]}
    try:
        receipt = client.send_work_card(
            **payload, card_instance_id=claim["intent"]["intent_id"]
        )
    except Exception as error:  # noqa: BLE001
        if _not_dispatched(error):
            withdrawn = state.withdraw_intent(run_id, _OPERATION, key, intent_payload)
            if withdrawn.get("status") == "WITHDRAWN":
                return {
                    "ok": False, "reason_code": "WORK_CARD_SEND_REJECTED",
                    "run_id": run_id, "retry_allowed": True, "detail": str(error),
                }
        return {"ok": False, "reason_code": "WORK_CARD_QUERY_REQUIRED", "run_id": run_id,
                "intent_id": claim["intent"]["intent_id"], "detail": str(error)}
    if receipt.get("card_id") != f"work-{run_id}":
        return {"ok": False, "reason_code": "WORK_CARD_QUERY_REQUIRED", "run_id": run_id,
                "intent_id": claim["intent"]["intent_id"]}
    state.receipt(claim["intent"]["intent_id"], receipt, [])
    return {"ok": True, "reason_code": "WORK_CARD_UPDATED", "run_id": run_id,
            "card_id": receipt["card_id"]}


def _card_payload(snapshot: dict[str, Any], group_id: str) -> dict[str, Any]:
    run_id = str(snapshot["run_id"])
    phase = str(snapshot["current_phase"])
    overall = snapshot.get("overall") or {}
    tasks = snapshot.get("tasks") or {}
    pipelines = snapshot.get("pipelines") or {}
    release = snapshot.get("release") or {}
    repositories = snapshot.get("repositories") or []
    next_action = snapshot.get("next_action") or {}
    module_items = [
        f"{item.get('module') or '-'}={item.get('status') or 'UNKNOWN'}"
        for item in repositories
    ]
    visible = []
    for index, item in enumerate(module_items):
        remaining = len(module_items) - index
        suffix = f"、另有{remaining}个模块" if remaining else ""
        if len("、".join(visible + [item]) + suffix) > 220:
            break
        visible.append(item)
    omitted = len(module_items) - len(visible)
    modules_line = "、".join(visible) or "-"
    if omitted:
        modules_line += f"、另有{omitted}个模块（详情见 status）"
    lines = [
        f"阶段 {phase} · {overall.get('done', 0)}/{overall.get('total', 0)}",
        f"流程 {phase_route(snapshot, max_chars=210)}",
        f"任务 {tasks.get('done', 0)}/{tasks.get('total', 0)} · 当前 {tasks.get('current') or '-'}",
        "模块 " + modules_line,
        f"流水线 {pipelines.get('done', 0)}/{pipelines.get('total', 0)} · {pipelines.get('status') or 'PENDING'}",
        f"发布 {release.get('status') or 'PENDING'} · 待处理 {','.join(release.get('waiting_for') or []) or '-'}",
        f"下一步 {next_action.get('owner') or '-'}：{str(next_action.get('text') or '-')[:180]}",
        f"run_id {run_id}",
    ]
    return {
        "run_id": run_id, "target_type": "group", "target_id": group_id,
        "title": "tom-autodev 工作进度", "question": "只读状态；审批请使用独立审批卡",
        "lines": lines,
    }


def refresh_all(orchestrator: Any, client: Any) -> list[dict[str, Any]]:
    """Refresh every live run without making the watcher own progress semantics."""
    outcomes = []
    for latest in orchestrator.state.latest_states():
        run_id = latest.get("run_id")
        if isinstance(run_id, str) and run_id:
            outcomes.append(refresh(orchestrator, client, run_id))
    return outcomes


def reconcile(
    orchestrator: Any,
    intent_id: str,
    outcome: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    card_id: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    """Close an unknown work-card delivery after an operator checks the remote card.

    The SDK exposes render but not a card lookup endpoint.  This command is therefore
    deliberately explicit: the operator records either the observed card identity or
    an audited abandonment.  It never guesses that a timeout meant success.
    """
    state = orchestrator.state
    intent = state.intent_by_id(intent_id) if callable(getattr(state, "intent_by_id", None)) else None
    if intent is None:
        for latest in state.latest_states():
            for candidate in state.pending_intents(latest.get("run_id")):
                if candidate.get("intent_id") == intent_id:
                    intent = candidate
                    break
            if intent is not None:
                break
    if not isinstance(intent, dict):
        return {"ok": False, "reason_code": "INTENT_NOT_FOUND", "intent_id": intent_id}
    if intent.get("operation") != _OPERATION:
        return {"ok": False, "reason_code": "WORK_CARD_INTENT_REQUIRED", "intent_id": intent_id}
    run_id = intent.get("run_id")
    if outcome == "delivered":
        expected = f"work-{run_id}"
        if not actor or not actor.strip() or not reason or not reason.strip():
            return {"ok": False, "reason_code": "WORK_CARD_RECONCILE_AUDIT_REQUIRED",
                    "intent_id": intent_id}
        if card_id != expected:
            return {"ok": False, "reason_code": "WORK_CARD_ID_MISMATCH", "intent_id": intent_id,
                    "expected_card_id": expected}
        response: dict[str, Any] = {
            "card_id": card_id, "created": False, "manually_reconciled": True,
            "actor": actor.strip(), "reason": reason.strip(),
        }
        if revision is not None:
            response["revision"] = revision
        try:
            receipt = state.receipt(intent_id, response, [])
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "intent_id": intent_id}
        return {"ok": True, "reason_code": "WORK_CARD_RECONCILED", "run_id": run_id,
                "intent_id": intent_id, "receipt": receipt}
    if outcome == "abandoned":
        if not reason or not actor:
            return {"ok": False, "reason_code": "WORK_CARD_ABANDON_REASON_REQUIRED",
                    "intent_id": intent_id}
        try:
            abandoned = state.abandon_intent(intent_id, reason, actor)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "intent_id": intent_id}
        return {"ok": True, "reason_code": "WORK_CARD_ABANDONED", "run_id": run_id,
                "intent_id": intent_id, **abandoned}
    return {"ok": False, "reason_code": "WORK_CARD_OUTCOME_INVALID", "intent_id": intent_id}


def _successful_receipt(item: dict[str, Any]) -> bool:
    intent = item.get("intent") if isinstance(item, dict) else None
    receipt = item.get("receipt") if isinstance(item, dict) else None
    response = receipt.get("response") if isinstance(receipt, dict) else None
    if not isinstance(response, dict):
        return False
    if response.get("reason_code") == "INTENT_ABANDONED" or response.get("ok") is False:
        return False
    run_id = intent.get("run_id") if isinstance(intent, dict) else None
    expected = f"work-{run_id}" if isinstance(run_id, str) and run_id else None
    return isinstance(expected, str) and response.get("card_id") == expected


def _not_dispatched(error: BaseException) -> bool:
    outcome = getattr(error, "outcome", None)
    if outcome == "REJECTED_BEFORE_SEND":
        return True
    # Argument binding and payload validation happen before the gateway request. A
    # local TypeError must not strand an intent as if the remote write were unknown.
    if isinstance(error, TypeError):
        return True
    return str(error) in _NOT_DISPATCHED
