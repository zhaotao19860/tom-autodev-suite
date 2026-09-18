from __future__ import annotations

from typing import Any


NON_REPAIRABLE_FAILURES = {
    "ENV_UNSATISFIED",
    "ENV_TRANSIENT",
    "BASELINE_UNVERIFIED",
    "REVISION_MISMATCH",
}

# A signature unresolved across at least this many distinct runs is escalated rather than
# repaired again: the same fix has already failed to hold across runs, so another repair
# round is unlikely to help.
CROSS_RUN_RECURRENCE_THRESHOLD = 2


def known_failure_verdict(case: dict[str, Any] | None) -> dict[str, str] | None:
    """A cross-run FailureCase verdict, or None to defer to the per-run next_action.

    Signature-first matching (Phase 3b): a failure the library has seen unresolved across
    >= CROSS_RUN_RECURRENCE_THRESHOLD distinct runs is sent to ARCHITECTURE_REVIEW instead
    of another blind repair. A resolved or first-time-cross-run signature returns None so
    the normal single-run repair policy still governs.
    """
    if not isinstance(case, dict) or case.get("resolved"):
        return None
    if case.get("distinct_runs", 0) >= CROSS_RUN_RECURRENCE_THRESHOLD:
        return {"action": "ARCHITECTURE_REVIEW", "reason_code": "KNOWN_CROSS_RUN_FAILURE"}
    return None


def next_action(
    history: list[dict[str, Any]], known_case: dict[str, Any] | None = None
) -> dict[str, str]:
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

    cross_run = known_failure_verdict(known_case)
    if cross_run is not None:
        return cross_run

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
