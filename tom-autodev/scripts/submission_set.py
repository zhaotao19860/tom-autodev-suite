"""One account of a multi-repository submission (MVP contract 3.2).

A reviewed change set can touch more than one repository (the business repo and the
product-test repo). Each repository is pushed as its own iCode intent; this module adds
the aggregate over them, derived from those intents' persisted states:

    SUBMITTED  every repository CONFIRMED with a receipt -- the only state that may
               enter IPIPE; recorded once, write-once (the CAS: a second, different
               record for the same attempt is refused by the store).
    PARTIAL    some repository is CONFIRMED and another is AMBIGUOUS or was refused --
               "C was approved" is not a reason to go on; F decides.
    PENDING    nothing decisive yet (claimed / unknown / not attempted): reconcile only
               the unknown repositories, never re-push a confirmed one.

An attempt is the (change_set_id, revision_set_id) pair. A code change produces a new
change set and so a new attempt; an attempt's record is never rewritten.
"""

from __future__ import annotations

from typing import Any

import intent_executor as ie

SUBMITTED = "SUBMITTED"
PARTIAL = "PARTIAL"
PENDING = "PENDING"


def attempt_key(run_id: str, change_set_id: str, revision_set_id: str) -> str:
    return f"submission-set:{run_id}:{change_set_id}:{revision_set_id}"


def repository_states(
    state_store: Any, run_id: str, change_set_ids: list[str], revision_set_id: str,
) -> list[dict[str, Any]]:
    """Per-repository state of the latest intent of each change set id of this attempt."""
    intents = [item for item in state_store.all_intents(run_id) if item["operation"] == "icode.submit"]
    rows = []
    for change_set_id in change_set_ids:
        prefix = f"icode.submit:{run_id}:{change_set_id}:{revision_set_id}"
        lineage = [item for item in intents
                   if item["idempotency_key"] == prefix
                   or item["idempotency_key"].startswith(prefix + ":after:")]
        if not lineage:
            rows.append({"change_set_id": change_set_id, "intent_state": "NOT_ATTEMPTED"})
            continue
        latest = lineage[-1]
        state = ie.intent_state(state_store, latest)
        done = state_store.result_by_idempotency_key(latest["idempotency_key"])
        response = (done or {}).get("receipt", {}).get("response") if done else None
        refused = isinstance(response, dict) and response.get("ok") is False and state == ie.CONFIRMED
        rows.append({
            "change_set_id": change_set_id,
            "module": (latest.get("payload") or {}).get("module"),
            "intent_id": latest["intent_id"],
            "intent_state": "REFUSED" if refused else state,
            "change_number": response.get("change_number") if isinstance(response, dict) else None,
        })
    return rows


def aggregate(rows: list[dict[str, Any]]) -> str:
    confirmed = [row for row in rows if row["intent_state"] == ie.CONFIRMED and row.get("change_number")]
    if rows and len(confirmed) == len(rows):
        return SUBMITTED
    final_bad = [row for row in rows if row["intent_state"] in {ie.AMBIGUOUS, "REFUSED"}]
    if confirmed and final_bad:
        return PARTIAL
    return PENDING


def evaluate(
    state_store: Any, run_id: str, change_set_ids: list[str], revision_set_id: str,
    primary_change_set_id: str,
) -> dict[str, Any]:
    """The aggregate of one attempt; records it once when it is SUBMITTED."""
    rows = repository_states(state_store, run_id, change_set_ids, revision_set_id)
    status = aggregate(rows)
    record = {"run_id": run_id, "change_set_id": primary_change_set_id,
              "revision_set_id": revision_set_id, "status": status, "repositories": rows}
    key = attempt_key(run_id, primary_change_set_id, revision_set_id)
    if status == SUBMITTED:
        stored = state_store.idempotency_result(key)
        if stored is None:
            state_store.save_idempotency_result(key, record)
        else:
            record = stored
    return record


def recorded(state_store: Any, run_id: str, change_set_id: str, revision_set_id: str) -> dict[str, Any] | None:
    return state_store.idempotency_result(attempt_key(run_id, change_set_id, revision_set_id))
