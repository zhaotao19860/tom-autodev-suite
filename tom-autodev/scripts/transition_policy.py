from __future__ import annotations

from typing import Any


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE": {"GRILL"},
    "GRILL": {"SPEC"},
    "SPEC": {"TASKS"},
    "TASKS": {"WORKSPACE"},
    # Back to TASKS because a DAG defect is only provable at binding time: the
    # WorkspaceGate binds one business repository per task, so a node that spans two
    # repositories cannot be bound and has to be re-cut. Without this edge the only
    # remedy is discarding a run that has three approved phases in it. Re-entry is not
    # a free rewind — it needs the G2-approved spec as evidence, and the new DAG must
    # win its own G3, so the discarded DAG stays in the ledger next to its approval.
    "WORKSPACE": {"PLAN", "TASKS"},
    "PLAN": {"IMPLEMENT"},
    "IMPLEMENT": {"REVIEW"},
    "REVIEW": {"WORKSPACE", "SUBMIT", "DIAGNOSE", "STOPPED"},
    "SUBMIT": {"IPIPE"},
    "IPIPE": {"RELEASE", "DIAGNOSE", "ENVIRONMENT_BLOCKED"},
    "RELEASE": {"RELEASE_SUCCESS", "DIAGNOSE", "ENVIRONMENT_BLOCKED"},
    # PLAN is reachable from DIAGNOSE only for a code-only repair, i.e. one whose
    # diagnosis carries repair_scope == "CODE_ONLY": the Spec and the DAG both still
    # hold, so re-entering at SPEC would ask three human gates to re-approve documents
    # nothing changed in. A repair that does touch the Spec or the task scope still
    # goes through SPEC. phase_protocol._completion_target() decides which, and the
    # PLAN re-entry keeps its own G4, so the repaired plan is approved on its own.
    "DIAGNOSE": {"SPEC", "PLAN", "ARCHITECTURE_REVIEW", "STOPPED"},
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
