from __future__ import annotations

from typing import Any


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE": {"GRILL"},
    "GRILL": {"SPEC"},
    "SPEC": {"TASKS"},
    "TASKS": {"WORKSPACE"},
    "WORKSPACE": {"PLAN"},
    "PLAN": {"IMPLEMENT"},
    "IMPLEMENT": {"REVIEW"},
    "REVIEW": {"WORKSPACE", "SUBMIT", "DIAGNOSE", "STOPPED"},
    "SUBMIT": {"IPIPE"},
    "IPIPE": {"RELEASE", "DIAGNOSE", "ENVIRONMENT_BLOCKED"},
    "RELEASE": {"RELEASE_SUCCESS", "DIAGNOSE", "ENVIRONMENT_BLOCKED"},
    "DIAGNOSE": {"SPEC", "ARCHITECTURE_REVIEW", "STOPPED"},
    "ARCHITECTURE_REVIEW": {"GRILL", "SPEC", "TASKS"},
    "ENVIRONMENT_BLOCKED": {"IPIPE", "STOPPED"},
    "RELEASE_SUCCESS": set(),
    "STOPPED": set(),
}

POLICY_ID = "transition-policy-v1"
TERMINAL_STATES = frozenset({"RELEASE_SUCCESS", "STOPPED"})


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

    def failure_target(self, current: str, reason_code: str) -> str:
        if reason_code == "REQUIREMENT_CHANGED":
            target = "GRILL"
        elif reason_code in {"ENV_UNSATISFIED", "ENV_TRANSIENT"}:
            target = "ENVIRONMENT_BLOCKED"
        elif reason_code in {"CODE_FAILURE", "TEST_FAILURE", "REVIEW_FAILED", "RELEASE_FAILED"}:
            target = "DIAGNOSE"
        else:
            target = "STOPPED"
        return target
