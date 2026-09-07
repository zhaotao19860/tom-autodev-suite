from __future__ import annotations

from typing import Any

from approval_contract import (
    ApprovalGatewayResult,
    gateway_result_matches_request,
    is_clean_initial_pending_result,
    parse_gateway_result,
)


class InfoflowApprovalClient:
    """Approval gateway boundary. A transport is required to keep network I/O explicit."""

    def __init__(self, transport: Any):
        self.transport = transport

    def request(self, request: dict[str, Any]) -> ApprovalGatewayResult:
        gateway_request = _gateway_request(request)
        if gateway_request is None:
            raise ValueError("APPROVAL_REQUEST_INVALID")
        response = self.transport.request(gateway_request)
        if not isinstance(response, dict):
            raise ValueError("APPROVAL_REQUEST_RESPONSE_INVALID")
        result = parse_gateway_result(_public_response(response))
        if (
            result is None
            or not is_clean_initial_pending_result(result)
            or not gateway_result_matches_request(result, gateway_request)
        ):
            raise ValueError("APPROVAL_REQUEST_RESPONSE_INVALID")
        return result

    def reconcile(self, request: dict[str, Any]) -> ApprovalGatewayResult | None:
        """A failed request may still have been delivered; only the transport knows.

        Without this the controller has to treat every transient delivery error as
        QUERY_REQUIRED and the gate wedges, because a replayed request could otherwise
        notify the approvers twice.
        """
        gateway_request = _gateway_request(request)
        if gateway_request is None or not hasattr(self.transport, "reconcile"):
            return None
        response = self.transport.reconcile(gateway_request)
        if not isinstance(response, dict):
            return None
        result = parse_gateway_result(_public_response(response))
        if result is None or not gateway_result_matches_request(result, gateway_request):
            return None
        return result

    def wait(self, request_id: str, timeout_seconds: float) -> ApprovalGatewayResult:
        if not isinstance(request_id, str) or not request_id or timeout_seconds < 0:
            raise ValueError("APPROVAL_WAIT_INVALID")
        response = self.transport.wait(request_id, timeout_seconds)
        if not isinstance(response, dict):
            raise ValueError("APPROVAL_WAIT_RESPONSE_INVALID")
        result = parse_gateway_result(_public_response(response))
        if result is None:
            raise ValueError("APPROVAL_WAIT_RESPONSE_INVALID")
        return result


def _public_response(response: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"token", "secret", "credential", "authorization", "password"}
    return {
        key: value
        for key, value in response.items()
        if isinstance(key, str) and not any(part in key.lower() for part in forbidden)
    }


def _gateway_request(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return None
    approval = request.get("approval")
    if approval is None:
        return dict(request) if request.get("approval_id") and request.get("input_hash") else None
    channel = request.get("channel")
    if not isinstance(approval, dict) or not isinstance(channel, str) or not channel:
        return None
    required = ("run_id", "approval_id", "input_hash", "member_policy")
    if any(not approval.get(field) for field in required):
        return None
    return {**approval, "channel": channel}
