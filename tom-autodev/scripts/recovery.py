from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lock_manager import LockManager
from state_store import StateStore


def partial_held(events: list[dict[str, Any]]) -> frozenset[str]:
    """Intents a recorded SUBMIT_PARTIAL route carried into DIAGNOSE, while not in SUBMIT.

    They stay open (no receipt) and keep blocking writes to their target through the
    claim prefix; they only stop holding the run, so the diagnosis a person needs to
    decide them can be produced. Back in SUBMIT they hold the run again.
    """
    if not events or events[-1].get("state") == "SUBMIT":
        return frozenset()
    held: set[str] = set()
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("reason_code") != "SUBMIT_PARTIAL":
            continue
        rows = ((payload.get("evidence") or {}).get("submission_set") or {}).get("repositories") or []
        held.update(row["intent_id"] for row in rows
                    if isinstance(row, dict) and isinstance(row.get("intent_id"), str))
    return frozenset(held)


class Recovery:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.state = StateStore(database_path)
        self.locks = LockManager(database_path, clock=clock)

    def resume(self, run_id: str, *, held_for_diagnosis: frozenset[str] = frozenset()) -> dict[str, Any]:
        """Inspect a checkpoint; READY does not mean a child phase was executed.

        Under evidence contract v2 an open KU/iCafe write is a side channel and blocks
        nothing. `held_for_diagnosis` names intents a failure route may carry into
        DIAGNOSE unresolved (contract 3.2 PARTIAL): they stay open and keep blocking every
        write to their target, they just do not stop the run from being diagnosed.
        """
        import workflow_spec
        events = self.state.events(run_id)
        checkpoint = events[-1] if events else None
        side_channel = workflow_spec.run_evidence_contract(events) == "v2"
        held_for_diagnosis = held_for_diagnosis | partial_held(events)
        uncertain = [
            intent for intent in self.state.pending_intents(run_id)
            if intent["intent_id"] not in held_for_diagnosis
            and not (side_channel and isinstance(intent.get("operation"), str)
                     and intent["operation"].split(".", 1)[0] in {"icafe", "ku", "knowledge"})
        ]
        actions = []
        for intent in uncertain:
            action = {
                "action": "QUERY_EXTERNAL_STATUS",
                "reason_code": "QUERY_REQUIRED",
                "intent_id": intent["intent_id"],
                "operation": intent["operation"],
                "idempotency_key": intent["idempotency_key"],
            }
            payload = intent.get("payload") if isinstance(intent, dict) else None
            if (
                isinstance(intent.get("operation"), str)
                and intent["operation"] == "approval.delivery.infoflow"
                and isinstance(payload, dict)
                and isinstance(payload.get("approval_id"), str)
            ):
                action.update(
                    {
                        "recovery_action": "CONFIRM_APPROVAL_REDELIVERY",
                        "approval_id": payload["approval_id"],
                        "message": (
                            "如流投递结果未知；确认未收到审批后，使用受控重发，"
                            "不会创建新的审批或改变审批材料。"
                        ),
                    }
                )
            actions.append(action)
        lock_records = self.locks.records()
        return {
            "run_id": run_id,
            "state": checkpoint["state"] if checkpoint is not None else "RUN_NOT_FOUND",
            "status": "QUERY_REQUIRED" if actions else "READY",
            "phase_complete": False,
            "execution_performed": False,
            "next_step": (
                "Resolve the listed uncertain intents before retrying their operations."
                if actions else
                "Drive this run with WorkerDriver.advance (or resume, for a settled "
                "approval-resume handoff): the worker owns sequencing — it runs every "
                "deterministic step and controller side effect itself and parks on an "
                "ApprovalJob (needs a human APPROVE bound to the exact input_hash) or a "
                "ProducerJob (needs model DraftContent, filled via submit-draft). Do not "
                "execute controllers or complete-phase by hand from the Agent; only fill a "
                "parked ProducerJob. Do not repeat resume for an unchanged action."
            ),
            "checkpoint": checkpoint,
            "events": events,
            "retry_allowed": not actions,
            "actions": actions,
            "active_locks": lock_records["active"],
            "stale_locks": lock_records["stale"],
            "incomplete_handoffs": self.state.incomplete_handoffs(run_id),
        }
