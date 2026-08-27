from __future__ import annotations

from datetime import datetime
from typing import Any

from evidence_policy import missing_evidence


class EvidenceGate:
    def check(
        self, action: str, context: dict[str, Any], current_state: str | None = None
    ) -> dict[str, Any]:
        reason_code, missing = missing_evidence(action, context, current_state)
        if reason_code:
            return _blocked(action, reason_code, missing)

        if context.get("blocking_findings"):
            return _blocked(action, "BLOCKING_FINDING")

        if context.get("repo_revisions") != context.get("evidence_revisions"):
            if context.get("repo_revisions") is not None or context.get("evidence_revisions") is not None:
                return _blocked(action, "REVISION_MISMATCH")

        if context.get("environment_fingerprint") != context.get("evidence_environment_fingerprint"):
            if context.get("environment_fingerprint") is not None or context.get("evidence_environment_fingerprint") is not None:
                return _blocked(action, "ENV_FINGERPRINT_MISMATCH")

        verification_created = context.get("verification_created_at")
        checkpoint_started = context.get("checkpoint_started_at")
        verification_time = _parse_time(verification_created) if verification_created is not None else None
        checkpoint_time = _parse_time(checkpoint_started) if checkpoint_started is not None else None
        if (verification_created is not None and verification_time is None) or (
            checkpoint_started is not None and checkpoint_time is None
        ):
            return _blocked(action, "INVALID_TIMESTAMP")
        if verification_time is not None and checkpoint_time is not None:
            if verification_time < checkpoint_time:
                return _blocked(action, "STALE_VERIFICATION")

        return {
            "passed": True,
            "action": action,
            "reason_code": "OK",
            "missing_evidence": [],
        }


def _blocked(action: str, reason_code: str, missing: list[str] | None = None) -> dict[str, Any]:
    return {
        "passed": False,
        "action": action,
        "reason_code": reason_code,
        "missing_evidence": missing or [],
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None
