"""The read-only policy check shared by run-bound execution entry points.

Apply ``guard_execution`` to entries that may write even on a replay (for example,
a completed Review can still build its submit descriptor). An entry with a proven
read-only replay may call ``execution_guard`` immediately after returning that
receipt. Inspection and stop deliberately remain available after policy drift.
"""

from __future__ import annotations

from functools import wraps
from inspect import signature
from typing import Any, Callable

import workflow_spec


def execution_guard(state: Any, run_id: str | None) -> dict[str, Any] | None:
    """Refuse fresh effects under a changed run policy, without changing any state.

    Unbound clients retain their existing persistence validation. New and legacy
    runs have no spec pin. Errors reading the ledger or hashing the policy are
    configuration failures and must propagate rather than masquerade as no drift.
    """
    if state is None or run_id is None:
        return None
    drift = workflow_spec.spec_drift(state.events(run_id))
    if drift is None:
        return None
    return {"ok": False, "reason_code": "WORKFLOW_SPEC_DRIFT", "run_id": run_id,
            "phase_complete": False, **drift}


def guard_execution(function: Callable[..., Any]) -> Callable[..., Any]:
    """Guard a controller method/function or a runtime bound to ``self.run_id``."""
    parameters = signature(function)

    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        arguments = parameters.bind(*args, **kwargs).arguments
        owner = arguments.get("self", arguments.get("orchestrator"))
        run_id = arguments.get("run_id") if "run_id" in parameters.parameters else owner.run_id
        blocked = execution_guard(owner.state, run_id)
        if blocked is not None:
            return blocked
        return function(*args, **kwargs)

    return guarded
