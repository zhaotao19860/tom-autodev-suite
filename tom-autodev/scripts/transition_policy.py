from __future__ import annotations

from typing import Any

import workflow_spec


# Derived from the single workflow spec (workflow_spec.py); editing an edge or a
# terminal state means editing the spec. tests/test_workflow_spec.py pins the mapping.
ALLOWED_TRANSITIONS: dict[str, set[str]] = workflow_spec.allowed_transitions()

POLICY_ID = "transition-policy-v1"
TERMINAL_STATES = workflow_spec.terminal_states()


class TransitionPolicy:
    def validate(self, current: str, next_state: str) -> dict[str, Any]:
        if current in TERMINAL_STATES:
            return {
                "allowed": False,
                "reason_code": "TERMINAL_STATE",
                "current_state": current,
                "next_state": next_state,
                "policy_id": POLICY_ID,
            }
        if next_state not in ALLOWED_TRANSITIONS.get(current, set()):
            return {
                "allowed": False,
                "reason_code": "INVALID_TRANSITION",
                "current_state": current,
                "next_state": next_state,
                "policy_id": POLICY_ID,
            }
        return {
            "allowed": True,
            "reason_code": "OK",
            "current_state": current,
            "next_state": next_state,
            "policy_id": POLICY_ID,
        }

    def validate_stop(self, current: str) -> dict[str, Any]:
        if current in TERMINAL_STATES:
            return self.validate(current, "STOPPED")
        return {
            "allowed": True,
            "reason_code": "OK",
            "current_state": current,
            "next_state": "STOPPED",
            "policy_id": POLICY_ID,
        }

    def failure_target(self, reason_code: str) -> str:
        # Single source of truth: the reason_code -> target mapping lives in
        # workflow_spec.FAILURE_TARGETS; this delegates rather than re-listing it.
        return workflow_spec.failure_target(reason_code)
