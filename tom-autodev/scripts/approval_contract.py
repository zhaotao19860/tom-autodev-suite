from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict, cast


STRICT_APPROVAL_TIMEOUT_SECONDS = 36000

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RESULT_FIELDS = frozenset(
    {
        "request_id",
        "approval_id",
        "run_id",
        "channel",
        "input_hash",
        "member_policy",
        "status",
        "created_at",
        "deadline_at",
        "heartbeat_at",
        "updated_at",
        "reply",
        "handoff",
        "reason_code",
    }
)


class ApprovalGatewayReply(TypedDict):
    reply_id: str
    decision: Literal["APPROVE", "REJECT"]
    responder: str
    received_at: str


class ApprovalGatewayHandoff(TypedDict):
    handoff_id: str
    status: Literal["PENDING"]


class ApprovalGatewayResult(TypedDict):
    request_id: str
    approval_id: str
    run_id: str
    channel: Literal["infoflow"]
    input_hash: str
    member_policy: dict[str, list[str]]
    status: Literal["PENDING", "APPROVE", "REJECT", "TIMEOUT"]
    created_at: str
    deadline_at: str
    heartbeat_at: str | None
    updated_at: str | None
    reply: ApprovalGatewayReply | None
    handoff: ApprovalGatewayHandoff | None
    reason_code: str | None


def parse_gateway_result(value: Any) -> ApprovalGatewayResult | None:
    if not isinstance(value, dict) or set(value) != _RESULT_FIELDS:
        return None
    if any(
        not isinstance(value.get(field), str) or not value[field]
        for field in (
            "request_id",
            "approval_id",
            "run_id",
            "input_hash",
            "created_at",
            "deadline_at",
        )
    ):
        return None
    if value.get("channel") != "infoflow" or not _valid_member_policy(value.get("member_policy")):
        return None
    if any(
        item is not None and not isinstance(item, str)
        for item in (value.get("heartbeat_at"), value.get("updated_at"), value.get("reason_code"))
    ):
        return None

    status = value.get("status")
    reply = value.get("reply")
    handoff = value.get("handoff")
    if status == "PENDING":
        if reply is not None or handoff is not None:
            return None
    elif status in {"APPROVE", "REJECT"}:
        if not _valid_reply(reply, status) or handoff is not None:
            return None
    elif status == "TIMEOUT":
        if reply is not None or not _valid_handoff(handoff):
            return None
    else:
        return None
    return cast(ApprovalGatewayResult, copy.deepcopy(value))


def gateway_result_matches_request(
    result: ApprovalGatewayResult, request: Any
) -> bool:
    if not isinstance(request, dict):
        return False
    if any(
        result[field] != request.get(field)
        for field in ("run_id", "approval_id", "channel", "input_hash")
    ):
        return False
    result_policy = _canonical_member_policy(result.get("member_policy"))
    request_policy = _canonical_member_policy(request.get("member_policy"))
    if result_policy is None or result_policy != request_policy:
        return False
    result_deadline = _normalize_deadline(result.get("deadline_at"))
    request_deadline = _normalize_deadline(request.get("deadline_at"))
    return result_deadline is not None and result_deadline == request_deadline


def is_clean_initial_pending_result(result: ApprovalGatewayResult) -> bool:
    return (
        result.get("status") == "PENDING"
        and result.get("reason_code") is None
        and result.get("reply") is None
        and result.get("handoff") is None
    )


def _valid_member_policy(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"comate", "infoflow"}
        and all(isinstance(value[channel], list) and value[channel] for channel in value)
        and all(
            isinstance(member, str) and _EMAIL.fullmatch(member)
            for members in value.values()
            for member in members
        )
    )


def _canonical_member_policy(value: Any) -> dict[str, list[str]] | None:
    if not _valid_member_policy(value):
        return None
    return {channel: sorted(value[channel]) for channel in sorted(value)}


def _normalize_deadline(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _valid_reply(value: Any, status: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"reply_id", "decision", "responder", "received_at"}
        and isinstance(value.get("reply_id"), str)
        and bool(value["reply_id"])
        and value.get("decision") == status
        and isinstance(value.get("responder"), str)
        and bool(_EMAIL.fullmatch(value["responder"]))
        and isinstance(value.get("received_at"), str)
        and bool(value["received_at"])
    )


def _valid_handoff(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"handoff_id", "status"}
        and isinstance(value.get("handoff_id"), str)
        and bool(value["handoff_id"])
        and value.get("status") == "PENDING"
    )
