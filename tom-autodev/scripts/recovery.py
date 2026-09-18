from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lock_manager import LockManager
from state_store import StateStore


class Recovery:
    def __init__(
        self,
        database_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.state = StateStore(database_path)
        self.locks = LockManager(database_path, clock=clock)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Inspect a checkpoint; READY does not mean a child phase was executed."""
        events = self.state.events(run_id)
        checkpoint = events[-1] if events else None
        uncertain = self.state.pending_intents(run_id)
        actions = [
            {
                "action": "QUERY_EXTERNAL_STATUS",
                "reason_code": "QUERY_REQUIRED",
                "intent_id": intent["intent_id"],
                "operation": intent["operation"],
                "idempotency_key": intent["idempotency_key"],
            }
            for intent in uncertain
        ]
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
