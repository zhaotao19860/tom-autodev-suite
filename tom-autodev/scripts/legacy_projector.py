"""Read-only projection of a run, independent of the live workflow (MVP §7).

Before any recovery or migration module is deleted, every historical run must still be
answerable: where it stands, what it waits on, what it produced. This projector reads
only the event log, the artifact store and the approval ledger. It does not consult the
live workflow spec, `next()`, migrations, re-pins or any remote system, so it keeps
working for runs whose pinned policy the current code no longer carries.

`drill(orchestrator)` runs it over every run in the store and compares the projected
state and open gates with the current `status` brief.
"""

from __future__ import annotations

from typing import Any

from approval_ledger import gate_of

_TERMINAL = frozenset({"RELEASE_SUCCESS", "STOPPED", "DONE", "RELEASED"})


def status(orchestrator: Any, run_id: str) -> dict[str, Any]:
    events = orchestrator.state.events(run_id)
    if not events:
        return {"run_id": run_id, "state": "RUN_NOT_FOUND"}
    intake = events[0].get("payload") or {}
    current = events[-1]
    terminal = current.get("state") in _TERMINAL
    approvals = orchestrator.approvals.for_run(run_id)
    # An unresolved card is open unless a later APPROVE of the same gate supersedes it.
    approved_at: dict[str, str] = {}
    for row in approvals:
        if row.get("effective_decision") == "APPROVE":
            gate = gate_of(row.get("action") or "")
            approved_at[gate] = max(approved_at.get(gate, ""), row.get("resolved_at") or "")
    open_gates = sorted({
        gate_of(row.get("action") or "")
        for row in approvals
        if not row.get("effective_decision") and not terminal
        and approved_at.get(gate_of(row.get("action") or ""), "") < (row.get("created_at") or "")
    })
    return {
        "run_id": run_id,
        "requirement_id": intake.get("requirement_id"),
        "project": intake.get("project"),
        "state": current.get("state"),
        "since": current.get("created_at"),
        "terminal": terminal,
        "evidence_contract": intake.get("evidence_contract") or "v1",
        "transitions": len(events) - 1,
        "open_gates": open_gates,
        "pending_intents": [] if terminal else [
            {"intent_id": item["intent_id"], "operation": item.get("operation")}
            for item in orchestrator.state.pending_intents(run_id)],
    }


def context(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """The run's status plus what it produced: artifacts by kind and decided approvals."""
    projected = status(orchestrator, run_id)
    if projected["state"] == "RUN_NOT_FOUND":
        return projected
    artifacts = [
        {"artifact_id": item.get("artifact_id"), "kind": item.get("kind"),
         "sha256": item.get("sha256")}
        for item in orchestrator.artifacts.artifacts_for_run(run_id)
    ]
    decisions = [
        {"gate": gate_of(row.get("action") or ""), "decision": row.get("effective_decision"),
         "input_hash": row.get("input_hash"), "resolved_at": row.get("resolved_at")}
        for row in orchestrator.approvals.for_run(run_id) if row.get("effective_decision")
    ]
    return {**projected, "artifacts": artifacts, "decisions": decisions}


def drill(orchestrator: Any) -> dict[str, Any]:
    """Project every run and compare it with the live `status` brief (read-only)."""
    from run_brief import build

    with orchestrator.state._connect() as connection:
        run_ids = [row[0] for row in connection.execute(
            "SELECT DISTINCT run_id FROM events ORDER BY run_id")]
    rows = []
    for run_id in run_ids:
        projected = status(orchestrator, run_id)
        try:
            brief = build(orchestrator, run_id, include_progress=False)
            live_state = brief.get("state")
            live_gates = sorted({gate_of(g.get("action") or "") for g in brief.get("waiting_on") or []})
            error = None
        except Exception as exc:  # noqa: BLE001 - the drill reports, it never raises
            live_state, live_gates, error = None, None, type(exc).__name__
        rows.append({
            "run_id": run_id, "requirement_id": projected.get("requirement_id"),
            "projected_state": projected["state"], "live_state": live_state,
            "projected_gates": projected.get("open_gates"), "live_gates": live_gates,
            "live_error": error,
            # The projector must always answer; agreement is required on state, and the
            # live brief may narrow open gates to the current phase's.
            "ok": projected["state"] != "RUN_NOT_FOUND"
            and (error is not None or (live_state == projected["state"]
                                       and set(live_gates or []) <= set(projected["open_gates"]))),
        })
    return {"runs": len(rows), "passed": sum(r["ok"] for r in rows), "rows": rows}
