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
        # The live attempt is the one the store would hand a retry: the chain past every
        # abandoned / proven-absent predecessor, not the last row by position.
        live = state_store.live_idempotency_key(prefix)
        latest = next((item for item in intents if item["idempotency_key"] == live), None)
        if latest is None:
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
        rows.append({
            "change_set_id": change_set_id,
            "module": (latest.get("payload") or {}).get("module"),
            "intent_id": latest["intent_id"],
            "intent_state": state,
            "change_number": response.get("change_number") if isinstance(response, dict) else None,
        })
    return rows


def aggregate(rows: list[dict[str, Any]]) -> str:
    # Any repository settled badly (AMBIGUOUS, refused) makes the attempt PARTIAL: reconcile
    # cannot turn it into anything else, whether or not a sibling is confirmed yet.
    if any(row["intent_state"] in {ie.AMBIGUOUS, ie.REFUSED} for row in rows):
        return PARTIAL
    if rows and all(row["intent_state"] == ie.CONFIRMED for row in rows):
        # A confirmed CR without a number cannot become a revision_set row; it is a broken
        # receipt, reported by evaluate, not a reason to keep waiting.
        return SUBMITTED if all(row.get("change_number") for row in rows) else PARTIAL
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
    if any(row["intent_state"] == ie.CONFIRMED and not row.get("change_number") for row in rows):
        record["reason_code"] = "RECEIPT_MISSING_CHANGE_NUMBER"
    key = attempt_key(run_id, primary_change_set_id, revision_set_id)
    if status == SUBMITTED:
        stored = state_store.idempotency_result(key)
        if stored is not None:
            return stored
        try:
            # Write-once: a concurrent, different SUBMITTED record for the attempt loses.
            state_store.save_idempotency_result(key, record)
        except ValueError:
            return {**record, "status": PARTIAL, "reason_code": "SUBMISSION_CONFLICT",
                    "recorded": state_store.idempotency_result(key)}
    return record


def recorded(state_store: Any, run_id: str, change_set_id: str, revision_set_id: str) -> dict[str, Any] | None:
    return state_store.idempotency_result(attempt_key(run_id, change_set_id, revision_set_id))
