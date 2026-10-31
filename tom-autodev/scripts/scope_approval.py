"""Scope approval in the worker (MVP §2): one card for a run of consecutive gates.

Each gate a scope covers is still computed, hash-bound and recorded exactly as before;
what changes is who answers it while the scope is being prepared. Instead of a person,
a gate of an open scope is answered by a *provisional* approval that only lets the
prepare segment continue to its next mechanical step. When the segment's last gate is
reached, one card is issued for the whole segment; its scope hash covers every gate
input of the segment, so the card approves exactly the artifacts it lists. Once that
card is approved, every gate of the segment gets its real, derived APPROVE
(`effective_channel = scope:<approval_id>`), and only then may the run cross the
segment's exit (the D segment's WORKSPACE controller, the C segment's iCode submission).

Scopes (gates in order; the last one ends the segment and raises the card):

    D  G1 (decision log) -> G2 (spec, with the DAG) -> G4 (workspace)
    C  G4 (task plan) -> G5 (change set) -> G7 (iCode submission + trigger)

Rules, from the MVP contract:
- a GRILL that has open questions stops and asks; the provisional answer is only for a
  completed decision log (its draft must be DECISIONS_RECORDED);
- a review that is not PASS raises no C card (the run routes to DIAGNOSE as before);
- the card's members are the intersection of the members of every gate it covers;
  if the intersection is empty, the gates are asked one by one;
- any change to an artifact changes its gate input hash, so the scope hash no longer
  matches and the prepared card does not apply.

A run opts in at INTAKE (`approval_mode: "scope"`); runs started before keep per-gate
approval. A provisional approval is recorded as its own ledger row with
`effective_channel = scope-pending:<scope>` and decision `PREPARED`, which no gate check
accepts as APPROVE.
"""

from __future__ import annotations

from typing import Any

SCOPES: dict[str, tuple[str, ...]] = {
    "D": ("G1", "G2", "G4"),
    "C": ("G4", "G5", "G7"),
}
POLICY_VERSION = "scope-worker.v1"


def policy_hash() -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps({"version": POLICY_VERSION, "scopes": SCOPES},
                                     sort_keys=True).encode()).hexdigest()


def enabled(events: list[dict[str, Any]]) -> bool:
    intake = (events[0].get("payload") or {}) if events else {}
    return intake.get("approval_mode") == "scope"


# Which state each scope gate is asked in. A gate asked anywhere else (the IPIPE
# trigger's G7, a repair round's G4) stays a per-gate approval.
_ASKED_IN = {
    "D": {"G1": "GRILL", "G2": "SPEC", "G4": "WORKSPACE"},
    "C": {"G4": "PLAN", "G5": "IMPLEMENT", "G7": "SUBMIT"},
}


def segment(events: list[dict[str, Any]], gate: str) -> str | None:
    """The scope this gate request belongs to at this point of the run, if any."""
    if not events or not enabled(events):
        return None
    state = events[-1].get("state")
    for name, asked in _ASKED_IN.items():
        if asked.get(gate) != state:
            continue
        if name == "D" and gate == "G4":
            # D ends at the first frontier's workspace; a later frontier's G4 is per gate.
            start = _segment_start(events, "D")
            index = next((i for i, e in enumerate(events) if e["event_id"] == start), 0)
            if any(e.get("state") == "PLAN" for e in events[index:]):
                return None
        if name == "C" and _repair_reentry(events):
            return None
        return name
    return None


def _repair_reentry(events: list[dict[str, Any]]) -> bool:
    """A frontier re-entered from DIAGNOSE is a repair round: its gates stay per gate."""
    plan = next((e for e in reversed(events) if e.get("state") in {"PLAN", "IMPLEMENT"}
                 and (e.get("payload") or {}).get("previous_state") in {"DIAGNOSE", "WORKSPACE"}), None)
    return bool(plan) and (plan.get("payload") or {}).get("previous_state") == "DIAGNOSE"


def last_gate(name: str) -> str:
    return SCOPES[name][-1]


def _scope_key(run_id: str, name: str, start: str) -> str:
    return f"scope.segment:{run_id}:{name}:{start}"


def _segment_start(events: list[dict[str, Any]], name: str) -> str:
    """The event id that opened the current instance of this segment."""
    if name == "D":
        start = None
        for event in events:
            if event.get("state") == "GRILL":
                start = event
        return (start or events[0])["event_id"]
    start = next((e for e in reversed(events) if e.get("state") == "PLAN"), None)
    return (start or events[-1])["event_id"]


def segment_start(events: list[dict[str, Any]], name: str) -> str:
    return _segment_start(events, name)


def collected(state: Any, run_id: str, name: str, start: str) -> list[tuple[str, str]]:
    """(gate, input_hash) the segment has prepared, newest per gate, in scope order."""
    rows = state.idempotency_results_with_prefix(f"{_scope_key(run_id, name, start)}:gate:")
    found: dict[str, str] = {}
    for _key, record in rows:
        found[str(record.get("gate"))] = str(record.get("input_hash"))
    return [(gate, found[gate]) for gate in SCOPES[name] if gate in found]


def record_gate(state: Any, run_id: str, name: str, start: str, gate: str, input_hash: str) -> None:
    key = f"{_scope_key(run_id, name, start)}:gate:{gate}:{input_hash}"
    if state.idempotency_result(key) is None:
        state.save_idempotency_result(key, {"gate": gate, "input_hash": input_hash})
