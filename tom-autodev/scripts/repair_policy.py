from __future__ import annotations

from typing import Any


NON_REPAIRABLE_FAILURES = {
    "ENV_UNSATISFIED",
    "ENV_TRANSIENT",
    "PIPELINE_TRANSIENT",
    "BASELINE_UNVERIFIED",
    "REVISION_MISMATCH",
}

# Failure classes that describe the environment, the platform or a transient pipeline
# condition. None of them is fixed by a business patch, so a diagnosis may not route
# them to REPAIR (ENVIRONMENT_REPAIR_FORBIDDEN). The authoritative class is the one the
# iPipe runtime recorded for the failure, not the one the diagnosis reports.
ENVIRONMENT_FAILURE_CLASSES = frozenset({
    "ENVIRONMENT_FAILURE",
    "ENV_UNSATISFIED",
    "ENV_TRANSIENT",
    "PIPELINE_TRANSIENT",
    "JOB_PARAMETER_OVERRIDE_UNSUPPORTED",
    "COMPILE_ONLY_TRIGGER_UNSUPPORTED",
})

# The subset of classes the *runtime* may assert with authority: produced only from
# structured platform fields, control-plane answers (job override / compile-only
# trigger unsupported, transient pipeline transport) or proven dependency fetch
# failures. "ENVIRONMENT_FAILURE" is deliberately absent: the runtime used to emit it
# from keyword matches ("timeout", "network"), and a stored legacy value is advisory.
RUNTIME_ENVIRONMENT_CLASSES = frozenset({
    "ENV_UNSATISFIED",
    "ENV_TRANSIENT",
    "PIPELINE_TRANSIENT",
    "JOB_PARAMETER_OVERRIDE_UNSUPPORTED",
    "COMPILE_ONLY_TRIGGER_UNSUPPORTED",
})

# Diagnosis routes that lead to changed business code (directly, or by re-planning or
# re-clarifying the requirement). With an environment classification none of them is
# allowed; STOPPED, DIAGNOSIS_INCOMPLETE and a human ARCHITECTURE_REVIEW remain.
ENVIRONMENT_FORBIDDEN_ROUTES = frozenset({"REPAIR", "SPEC", "GRILL"})


def runtime_environment_class(value: object) -> bool:
    """Whether a runtime-recorded class is an authoritative environment class."""
    return isinstance(value, str) and (value in RUNTIME_ENVIRONMENT_CLASSES or value.startswith("ENV_"))


# Absolute bound on confirmed repair rounds for one run, across every architecture
# review epoch and including rounds inherited through adopt-code-only-repair.
REPAIR_ROUND_LIMIT = 5

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

    # Two different counters (SKILL.md "Hard Boundaries", failure-taxonomy.md):
    # REPAIR_LIMIT counts every confirmed round the run has spent, in every epoch and
    # including inherited ones; an approved ARCHITECTURE_REVIEW does not refill it.
    # NO_PROGRESS and THREE_FAILED_FIXES only look at the rounds since the latest
    # architecture review (`epoch`), which is what that review resets. A round without
    # an `epoch` is epoch 0, so a history without reviews behaves exactly as before.
    all_rounds = [entry for entry in history if entry.get("diagnosis_confirmed")]
    if len(all_rounds) >= REPAIR_ROUND_LIMIT:
        return {"action": "STOP", "reason_code": "REPAIR_LIMIT"}
    epoch = latest.get("epoch", 0)
    repair_rounds = [
        entry for entry in all_rounds
        if not entry.get("inherited") and entry.get("epoch", 0) == epoch
    ]

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
