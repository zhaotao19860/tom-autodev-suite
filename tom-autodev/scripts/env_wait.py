"""ENV_WAIT (MVP §4): wait for an environment the run cannot fix, with a recheck schedule.

A manual stage whose input is another module's product that does not exist yet (the
branch has not released it, the compile-only fallback is not available) used to fail the
controller step on every `continue`. Nothing in the run can change that; a person or
another pipeline has to. The wait is recorded durably instead:

    wait_reason       the reason code the check returned
    next_actor        who is expected to unblock it
    recheck_after     before this, `continue` parks without asking the platform again
    max_wait          past this, the run stops (ENV_WAIT_EXPIRED)
    evidence_refresh  which check is repeated when the wait is rechecked

Only a fresh check that passes ends the wait; nobody can declare it over. The run stays
in IPIPE: the wait is a durable sub-state of the stage it blocks, not a new workflow edge,
so pinned runs keep their workflow spec hash.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Reasons a stage's input is unavailable for a cause outside the run.
WAIT_REASONS: dict[str, str] = {
    "PRODUCT_URL_NOT_PUBLISHED": "目标分支尚未发布该模块产物",
    "COMPILE_ONLY_TRIGGER_UNSUPPORTED": "未改动模块在目标分支没有可用发布产物，且无法只编译不发布",
    "PRODUCT_DOWNLOAD_UNVERIFIED": "产物下载地址尚不存在（404）",
}

# A download check that failed for another reason (401/403 is a permission problem, 5xx or
# no status is not yet known) is not proof the dependency is missing (C-09, ENV-001).
_ABSENT_STATUSES = frozenset({404, 410})


def waitable(check: dict[str, Any]) -> bool:
    """Whether a failed stage-input check proves the dependency is not there yet."""
    reason = check.get("reason_code")
    if reason not in WAIT_REASONS:
        return False
    if reason == "PRODUCT_DOWNLOAD_UNVERIFIED":
        # Every candidate must itself report 404/410; one without a status is unknown (C-09).
        candidates = check.get("candidates")
        if isinstance(candidates, list) and candidates:
            return all(isinstance(item, dict) and item.get("http_status") in _ABSENT_STATUSES
                       for item in candidates)
        statuses = set(check.get("http_statuses") or [])
        return bool(statuses) and statuses <= _ABSENT_STATUSES
    return True


def expired_unstopped(state: Any, run_id: str, stage_build_id: str) -> dict[str, Any] | None:
    """An EXPIRED end whose STOP may not have landed (crash between the two writes, C-08)."""
    reader = getattr(state, "idempotency_results_with_prefix", None)
    if not callable(reader):
        return None
    ended = [r for key, r in reader(_key(run_id, stage_build_id) + ":") if ":end:" in key]
    return next((r for r in ended if r.get("outcome") == "EXPIRED"), None)

RECHECK_AFTER = timedelta(minutes=30)
MAX_WAIT = timedelta(hours=72)


def _key(run_id: str, stage_build_id: str) -> str:
    return f"env.wait:{run_id}:{stage_build_id}"


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def opened(state: Any, run_id: str, stage_build_id: str) -> dict[str, Any] | None:
    """The open wait of this stage, or None (never opened, or already ended)."""
    reader = getattr(state, "idempotency_results_with_prefix", None)
    if not callable(reader):
        return None
    rows = reader(_key(run_id, stage_build_id) + ":")
    opens = [record for key, record in rows if ":open:" in key]
    if not opens:
        return None
    record = opens[-1]
    ended = [r for key, r in rows if ":end:" in key]
    return None if any(r.get("wait_id") == record.get("wait_id") for r in ended) else record


def open_wait(
    state: Any, run_id: str, stage_build_id: str, check: dict[str, Any], *,
    module: Any = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Record a wait for this stage (idempotent while one is open)."""
    current = opened(state, run_id, stage_build_id)
    if current is not None:
        return current
    started = _now(now)
    reason = str(check.get("reason_code") or "")
    target = check.get("product_target") if isinstance(check.get("product_target"), dict) else {}
    record = {
        "wait_id": f"{stage_build_id}:{started.strftime('%Y%m%dT%H%M%S%fZ')}",
        "run_id": run_id, "stage_build_id": stage_build_id, "module": module,
        "wait_reason": reason, "wait_reason_text": WAIT_REASONS.get(reason, reason),
        "next_actor": _next_actor(target),
        "product_target": target or None,
        "opened_at": started.isoformat(),
        "recheck_after": (started + RECHECK_AFTER).isoformat(),
        "max_wait": (started + MAX_WAIT).isoformat(),
        "evidence_refresh": "stage_parameters",
        "checks": 1,
    }
    state.save_idempotency_result(f"{_key(run_id, stage_build_id)}:open:{record['wait_id']}", record)
    return record


def _next_actor(target: dict[str, Any]) -> str:
    module = target.get("module")
    branch = target.get("branch")
    if module and branch:
        return f"{module} 在分支 {branch} 上完成一次 BranchPipeline 发布（合入其 CR 并完成发布段）"
    return "环境负责人"


def due(record: dict[str, Any], now: datetime | None = None) -> str:
    """WAIT (not yet), RECHECK, or EXPIRED."""
    current = _now(now)
    expires = _parse(record.get("max_wait"))
    if expires is not None and current >= expires:
        return "EXPIRED"
    recheck = _parse(record.get("recheck_after"))
    if recheck is not None and current < recheck:
        return "WAIT"
    return "RECHECK"


def still_waiting(
    state: Any, record: dict[str, Any], check: dict[str, Any], now: datetime | None = None,
) -> dict[str, Any]:
    """A recheck that did not clear: push recheck_after forward (append-only)."""
    current = _now(now)
    checks = int(record.get("checks") or 1) + 1
    updated = {**record, "checks": checks, "last_check_at": current.isoformat(),
               "last_check_reason": check.get("reason_code"),
               "recheck_after": (current + RECHECK_AFTER).isoformat()}
    state.save_idempotency_result(
        f"{_key(record['run_id'], record['stage_build_id'])}:recheck:{record['wait_id']}:{checks}", updated)
    return updated


def latest(state: Any, record: dict[str, Any]) -> dict[str, Any]:
    """The newest schedule of an open wait (its last recheck, else the opening)."""
    rows = state.idempotency_results_with_prefix(
        f"{_key(record['run_id'], record['stage_build_id'])}:recheck:{record['wait_id']}:")
    same = [r for _key_, r in rows]
    return max(same, key=lambda r: int(r.get("checks") or 0)) if same else record


def end_wait(
    state: Any, record: dict[str, Any], outcome: str, now: datetime | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close the wait: CLEARED (a fresh check passed) or EXPIRED."""
    current = _now(now)
    ended = {"wait_id": record.get("wait_id"), "outcome": outcome,
             "ended_at": current.isoformat(), "checks": record.get("checks"),
             **({"evidence": evidence} if evidence else {})}
    state.save_idempotency_result(
        f"{_key(record['run_id'], record['stage_build_id'])}:end:{record['wait_id']}", ended)
    return ended


def open_waits(state: Any, run_id: str) -> list[dict[str, Any]]:
    """Every open wait of the run, with its current schedule (for status)."""
    rows = state.idempotency_results_with_prefix(f"env.wait:{run_id}:")
    stages = sorted({str(r.get("stage_build_id")) for key, r in rows if ":open:" in key})
    found = []
    for stage in stages:
        record = opened(state, run_id, stage)
        if record is not None:
            found.append(latest(state, record))
    return found
