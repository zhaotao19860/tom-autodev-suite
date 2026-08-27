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
            "checkpoint": checkpoint,
            "events": events,
            "retry_allowed": not actions,
            "actions": actions,
            "active_locks": lock_records["active"],
            "stale_locks": lock_records["stale"],
            "incomplete_handoffs": self.state.incomplete_handoffs(run_id),
        }
