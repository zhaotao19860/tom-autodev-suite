from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from approval_contract import ApprovalGatewayResult, parse_gateway_result


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PendingApprovalGateway:
    """Small in-memory extraction of the old gateway's request/reply/wait lifecycle."""

    def __init__(self, clock: Callable[[], datetime] | None = None, state_store: Any | None = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state_store = state_store
        self._requests: dict[str, dict[str, Any]] = {}

    def request(self, request: dict[str, Any]) -> ApprovalGatewayResult:
        if not _valid_request_envelope(request):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        if self.state_store is None:
            raise ValueError("APPROVAL_STATE_STORE_REQUIRED")
        request_id = request.get("request_id") or uuid.uuid4().hex
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("APPROVAL_REQUEST_INVALID")
        deadline = _parse_deadline(request["deadline_at"])
        now = self.clock()
        record = {
            "request_id": request_id,
            "approval_id": request.get("approval_id"),
            "run_id": request.get("run_id"),
            "channel": request.get("channel"),
            "input_hash": request.get("input_hash"),
            "member_policy": copy.deepcopy(request["member_policy"]),
            "status": "PENDING",
            "created_at": now.isoformat(),
            "deadline_at": deadline.isoformat(),
            "heartbeat_at": None,
            "updated_at": None,
            "reply": None,
            "handoff": None,
            "reason_code": None,
        }
        existing = self._requests.get(request_id)
        if existing is not None:
            if any(existing[key] != record[key] for key in ("approval_id", "run_id", "channel", "input_hash", "member_policy", "deadline_at")):
                raise ValueError("APPROVAL_REQUEST_CONFLICT")
            return _public_result(existing)
        self._requests[request_id] = record
        return _public_result(record)

    def wait(self, request_id: str, timeout_seconds: float, *, now: datetime | None = None) -> ApprovalGatewayResult | None:
        del timeout_seconds  # A caller wait bound must never mutate the approved deadline.
        record = self._requests.get(request_id)
        if record is None:
            return None
        observed = now or self.clock()
        self._transition_timeout(record, observed)
        return _public_result(record)

    def reply(self, request_id: str, response: dict[str, Any]) -> ApprovalGatewayResult | None:
        record = self._requests.get(request_id)
        if record is None:
            return None
        self._transition_timeout(record, self.clock())
        if record["status"] != "PENDING":
            return _public_result(record)
        if response.get("run_id") != record["run_id"] or response.get("approval_id") != record["approval_id"] or response.get("channel") != record["channel"]:
            return _public_result({**record, "reason_code": "APPROVAL_ENVELOPE_INVALID"})
        if response.get("input_hash") != record["input_hash"]:
            return _public_result({**record, "reason_code": "APPROVAL_INPUT_MISMATCH"})
        if response.get("responder") not in record["member_policy"].get(record["channel"], []):
            return _public_result({**record, "reason_code": "APPROVAL_RESPONDER_UNAUTHORIZED"})
        decision = response.get("decision")
        if decision not in {"APPROVE", "REJECT"}:
            return _public_result({**record, "reason_code": "APPROVAL_DECISION_INVALID"})
        received_at = self.clock().isoformat()
        record["status"] = decision
        record["reply"] = {
            "reply_id": uuid.uuid4().hex,
            "decision": decision,
            "responder": response.get("responder"),
            "received_at": received_at,
        }
        record["updated_at"] = received_at
        return _public_result(record)

    def timeout(self, request_id: str, *, now: datetime | None = None) -> ApprovalGatewayResult | None:
        record = self._requests.get(request_id)
        if record is None:
            return None
        observed = now or self.clock()
        if record["status"] != "PENDING":
            return _public_result({**record, "reason_code": "APPROVAL_ALREADY_RESOLVED"})
        if observed < _parse_deadline(record["deadline_at"]):
            return _public_result({**record, "reason_code": "APPROVAL_TIMEOUT_EARLY"})
        self._transition_timeout(record, observed)
        return _public_result(record)

    def heartbeat(self, request_id: str, *, observed_at: str | None = None) -> ApprovalGatewayResult | None:
        record = self._requests.get(request_id)
        if record is None:
            return None
        record["heartbeat_at"] = observed_at or self.clock().isoformat()
        return _public_result(record)

    def _transition_timeout(self, record: dict[str, Any], observed: datetime) -> None:
        if record["status"] != "PENDING" or observed < _parse_deadline(record["deadline_at"]):
            return
        record["status"] = "TIMEOUT"
        record["updated_at"] = observed.isoformat()
        record["reason_code"] = "APPROVAL_TIMEOUT"
        record["handoff"] = {
            "handoff_id": f"approval-timeout-{record['approval_id']}",
            "status": "PENDING",
        }
        self.state_store.record_handoff(
            record["run_id"],
            record["handoff"]["handoff_id"],
            {"approval_id": record["approval_id"], "input_hash": record["input_hash"], "reason_code": "APPROVAL_TIMEOUT"},
        )


def _valid_request_envelope(request: Any) -> bool:
    if not isinstance(request, dict):
        return False
    required = ("run_id", "approval_id", "channel", "input_hash", "deadline_at")
    if any(not isinstance(request.get(key), str) or not request[key] for key in required):
        return False
    if request["channel"] != "infoflow":
        return False
    try:
        _parse_deadline(request["deadline_at"])
    except ValueError:
        return False
    policy = request.get("member_policy")
    return (
        isinstance(policy, dict)
        and set(policy) == {"comate", "infoflow"}
        and all(isinstance(policy.get(channel), list) and policy[channel] for channel in policy)
        and all(
            isinstance(member, str) and _EMAIL.fullmatch(member)
            for members in policy.values()
            for member in members
        )
    )


def _parse_deadline(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("APPROVAL_DEADLINE_INVALID") from None
    if parsed.tzinfo is None:
        raise ValueError("APPROVAL_DEADLINE_INVALID")
    return parsed.astimezone(timezone.utc)


def _public_result(record: dict[str, Any]) -> ApprovalGatewayResult:
    result = parse_gateway_result(record)
    if result is None:
        raise RuntimeError("APPROVAL_GATEWAY_RESULT_INVALID")
    return result
