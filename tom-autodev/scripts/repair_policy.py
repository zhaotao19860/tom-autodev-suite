from __future__ import annotations

from typing import Any


NON_REPAIRABLE_FAILURES = {
    "ENV_UNSATISFIED",
    "ENV_TRANSIENT",
    "BASELINE_UNVERIFIED",
    "REVISION_MISMATCH",
}


def next_action(history: list[dict[str, Any]]) -> dict[str, str]:
    if not history:
        return {"action": "STOP", "reason_code": "DIAGNOSIS_INCOMPLETE"}

    latest = history[-1]
    failure_class = latest.get("failure_class")
    if failure_class in NON_REPAIRABLE_FAILURES:
        return {"action": "STOP", "reason_code": str(failure_class)}
    if not latest.get("diagnosis_confirmed"):
        return {"action": "STOP", "reason_code": "DIAGNOSIS_INCOMPLETE"}

    repair_rounds = [entry for entry in history if entry.get("diagnosis_confirmed")]
    if len(repair_rounds) >= 5:
        return {"action": "STOP", "reason_code": "REPAIR_LIMIT"}

    if _same_signature_without_progress(repair_rounds[-2:]):
        return {"action": "STOP", "reason_code": "NO_PROGRESS"}

    if len(repair_rounds) >= 3 and all(not entry.get("resolved") for entry in repair_rounds[-3:]):
        return {
            "action": "ARCHITECTURE_REVIEW",
            "reason_code": "THREE_FAILED_FIXES",
        }

    return {"action": "REPAIR", "reason_code": "DIAGNOSIS_CONFIRMED"}


def _same_signature_without_progress(rounds: list[dict[str, Any]]) -> bool:
    if len(rounds) != 2:
        return False
    first, second = rounds
    signature = first.get("failure_signature")
    return bool(
        signature
        and signature == second.get("failure_signature")
        and not first.get("progress")
        and not second.get("progress")
    )
