from __future__ import annotations

from execution_guard import execution_guard

import hashlib
from datetime import datetime, timezone
from typing import Any

from approval_contract import ApprovalGatewayResult, parse_gateway_result
from collaboration import group_id_for_run


_RECORD_KEY = "approval.gateway.infoflow"
_REPLY_KEY = "approval.gateway.infoflow.reply"


def deliver_markdown(client: Any, state_store: Any, run_id: Any, recipients: list[str], render: Any) -> dict[str, Any]:
    """Send a run's message where its people are: the group, else private chats.

    `render` takes whether the message will be mentioned into a group, because a
    group message has to name the people it is asking; the `@` tokens must be in the
    body for the mention to read as anything.
    """
    group_id = group_id_for_run(state_store, run_id)
    addressees = sorted(recipients)
    content = render(group_id is not None)
    if group_id is None:
        return client.send_markdown(addressees, content)
    # A group mention only resolves against the uuap prefix; passing the full address
    # sends the message but silently mentions nobody.
    receipt = client.send_group_markdown(group_id, content, [_uuap(item) for item in addressees])
    return {
        "message_key": receipt.get("message_id") or receipt.get("message_key"),
        "recipients": addressees,
        "group_id": group_id,
    }


def mention_line(recipients: list[str]) -> str:
    return " ".join(f"@{_uuap(recipient)}" for recipient in sorted(recipients))


def _deliver_approval_card(client: Any, state_store: Any, gateway_request: dict[str, Any]) -> Any:
    """Add the one-tap button card next to the markdown card, when it is possible.

    Only a collaboration group is targeted: that path is the one the buttons were
    verified on, and it is also the surface where the typing was painful. The card is
    an accelerator, not the record — the markdown message still carries the links,
    hashes and documents — so any failure here is swallowed and reported in the stored
    receipt instead of failing the gate.
    """
    group_id = group_id_for_run(state_store, gateway_request["run_id"])
    if group_id is None or not hasattr(client, "send_approval_card"):
        return None
    evidence = gateway_request.get("evidence")
    gate = evidence.get("gate") if isinstance(evidence, dict) else None
    gate = gate if isinstance(gate, dict) else {}
    action = str(gateway_request.get("action") or "gate")
    lines = [
        f"需求 {_safe(_evidence(evidence, 'card_id'))} {_safe(_evidence(evidence, 'card_title'))}".strip(),
        f"本次审批 {_line(gate.get('subject'))}" if _line(gate.get("subject")) else "",
        f"批准后 {_line(gate.get('effect'))}" if _line(gate.get("effect")) else "",
        f"截止 {_moment(gateway_request['deadline_at'])}",
    ]
    try:
        return client.send_approval_card(
            approval_id=gateway_request["approval_id"],
            target_type="group",
            target_id=group_id,
            title=f"tom-autodev {action} 审批",
            question="点「同意」或「驳回」即可，无需再输入 approval_id；详情见上一条消息。",
            lines=[line for line in lines if line],
        )
    except Exception as error:  # noqa: BLE001 - the card is best effort; markdown is authoritative
        return {"error": str(error)}


def _uuap(recipient: str) -> str:
    return recipient.split("@", 1)[0] if isinstance(recipient, str) else ""


class ComateApprovalClient:
    """The comate channel is this CLI session, so delivery is the printed request."""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval = payload.get("approval") if isinstance(payload, dict) else None
        if not isinstance(approval, dict):
            raise ValueError("APPROVAL_REQUEST_INVALID")
        return {
            "channel": "comate",
            "approval_id": approval.get("approval_id"),
            "run_id": approval.get("run_id"),
            "input_hash": approval.get("input_hash"),
            "deadline_at": approval.get("deadline_at"),
            "surface": "comate-cli",
        }


class InfoflowApprovalTransport:
    """Durable Infoflow approval delivery behind `InfoflowApprovalClient`.

    `PendingApprovalGateway` keeps its records in memory, which is fine in-process but
    unusable from a CLI where every command is a fresh process: the pending request
    would vanish before anyone could answer it. Records live in the state store here,
    under a request id derived from the approval and its bound input hash, so a retry
    re-reads the same pending request instead of notifying the approvers twice.

    Replies are not polled either: the installed Infoflow scripts expose only
    `getSingleMsg` by message id, with no inbound listing or callback. A
    `reply_consumer` (see `clients.infoflow_reply_client`) reads the journal that the
    WebSocket relay writes and returns a decision that is already bound to this
    approval; without one the transport reports PENDING until the bound deadline
    turns it into a TIMEOUT handoff, and a human can still land the decision through
    `tom-autodev approve`.
    """

    def __init__(
        self,
        state_store: Any,
        notify_client: Any,
        *,
        clock: Any | None = None,
        reply_consumer: Any | None = None,
    ):
        self.state = state_store
        self.notify_client = notify_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reply_consumer = reply_consumer

    def request(self, gateway_request: dict[str, Any]) -> dict[str, Any]:
        blocked = execution_guard(self.state, gateway_request.get("run_id") if isinstance(gateway_request, dict) else None)
        if blocked is not None:
            return blocked
        if not isinstance(gateway_request, dict):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        required = ("run_id", "approval_id", "input_hash", "deadline_at", "member_policy")
        if any(not gateway_request.get(field) for field in required):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        if gateway_request.get("channel") != "infoflow":
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        recipients = gateway_request["member_policy"].get("infoflow")
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("APPROVAL_ENVELOPE_INVALID")

        request_id = _request_id(gateway_request)
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is not None:
            return self._resolved(stored["record"])
        delivered_content: list[str] = []

        def render(mention: bool) -> str:
            card = _approval_markdown(gateway_request, sorted(recipients), mention=mention)
            delivered_content.append(card)
            return card

        receipt = deliver_markdown(
            self.notify_client,
            self.state,
            gateway_request["run_id"],
            sorted(recipients),
            render,
        )
        content = delivered_content[-1]
        card = _deliver_approval_card(
            self.notify_client, self.state, gateway_request
        )
        record = {
            "request_id": request_id,
            "approval_id": gateway_request["approval_id"],
            "run_id": gateway_request["run_id"],
            "channel": "infoflow",
            "input_hash": gateway_request["input_hash"],
            "member_policy": {
                channel: sorted(members)
                for channel, members in gateway_request["member_policy"].items()
            },
            "status": "PENDING",
            "created_at": self.clock().isoformat(),
            "deadline_at": _deadline(gateway_request["deadline_at"]).isoformat(),
            "heartbeat_at": None,
            "updated_at": None,
            "reply": None,
            "handoff": None,
            "reason_code": None,
        }
        self.state.save_idempotency_result(
            f"{_RECORD_KEY}:{request_id}",
            {
                "record": record,
                "delivery": receipt,
                # Kept so a reminder repeats the exact card the approvers already saw,
                # and so a timed-out gate can be reissued without rebuilding evidence.
                "content": content,
                "evidence": gateway_request.get("evidence") or {},
                # Best effort: a gate that only got the markdown is still answerable by
                # typing, so a missing card must never look like a failed delivery.
                "card": card,
            },
        )
        return self._resolved(record)

    def delivered(self, request_id: str) -> dict[str, Any] | None:
        """The card and evidence a request was delivered with, when they were stored."""
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is None:
            return None
        evidence = stored.get("evidence")
        return {
            "content": stored.get("content") or "",
            "evidence": evidence if isinstance(evidence, dict) else {},
        }

    def reconcile(self, gateway_request: dict[str, Any]) -> dict[str, Any] | None:
        """Report an already-delivered request so a failed send is never replayed blind."""
        if not isinstance(gateway_request, dict) or any(
            not gateway_request.get(field) for field in ("run_id", "approval_id", "input_hash")
        ):
            return None
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{_request_id(gateway_request)}")
        return self._resolved(stored["record"]) if stored is not None else None

    def wait(self, request_id: str, timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds  # A caller's wait bound must never move the approved deadline.
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is None:
            raise ValueError("APPROVAL_WAIT_RESPONSE_INVALID")
        return self._resolved(stored["record"])

    def _resolved(self, record: dict[str, Any]) -> ApprovalGatewayResult:
        # Re-reading a delivered card can persist its new reply or timeout
        # handoff. Treat request/reconcile/wait as execution at this boundary.
        blocked = execution_guard(self.state, record.get("run_id"))
        if blocked is not None:
            return blocked
        record = self._with_reply(record)
        observed = self.clock()
        if record["status"] == "PENDING" and observed >= _deadline(record["deadline_at"]):
            handoff_id = f"approval-timeout-{record['approval_id']}"
            record = {
                **record,
                "status": "TIMEOUT",
                "updated_at": observed.isoformat(),
                "reason_code": "APPROVAL_TIMEOUT",
                "handoff": {"handoff_id": handoff_id, "status": "PENDING"},
            }
            self.state.record_handoff(
                record["run_id"],
                handoff_id,
                {
                    "approval_id": record["approval_id"],
                    "input_hash": record["input_hash"],
                    "reason_code": "APPROVAL_TIMEOUT",
                },
            )
        result = parse_gateway_result(record)
        if result is None:
            raise RuntimeError("APPROVAL_GATEWAY_RESULT_INVALID")
        return result

    def _with_reply(self, record: dict[str, Any]) -> dict[str, Any]:
        """Apply the decision that the reply consumer already bound to this request.

        The decision is stored once under its own key: the pending record is
        write-once, and a landed decision must survive the process that read it so a
        later `await-approval` sees the same answer instead of re-reading the journal.
        """
        if record["status"] != "PENDING":
            return record
        key = f"{_REPLY_KEY}:{record['request_id']}"
        stored = self.state.idempotency_result(key)
        if stored is None:
            if self.reply_consumer is None:
                return record
            reply = self.reply_consumer.resolve(record)
            if reply is None:
                return record
            stored = self.state.save_idempotency_result(key, {"reply": reply})
        reply = stored["reply"]
        return {
            **record,
            "status": reply["decision"],
            "reply": reply,
            "updated_at": reply["received_at"],
        }


def _request_id(gateway_request: dict[str, Any]) -> str:
    identity = "|".join(
        str(gateway_request[field]) for field in ("run_id", "approval_id", "input_hash")
    )
    return f"infoflow-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def _deadline(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("APPROVAL_DEADLINE_INVALID") from None
    if parsed.tzinfo is None:
        raise ValueError("APPROVAL_DEADLINE_INVALID")
    return parsed.astimezone(timezone.utc)


def _approval_markdown(
    gateway_request: dict[str, Any], recipients: list[str], *, mention: bool = False
) -> str:
    """Render the gate as something a person can act on from a phone.

    The decision line has to be copyable on its own, so the ids that bind it live in
    their own block instead of being mixed into prose. In a group the card also has
    to name the approvers, otherwise nobody can tell whether it is addressed to them;
    a single chat already addresses each of them.
    """
    approval_id = gateway_request["approval_id"]
    run_id = gateway_request["run_id"]
    input_hash = gateway_request["input_hash"]
    action = str(gateway_request.get("action") or "gate")
    evidence = gateway_request.get("evidence")
    lines = [
        f"## tom-autodev {action} 审批",
        "",
    ]
    if mention and recipients:
        lines += [f"**待审批** {mention_line(recipients)}", ""]
    lines += [
        f"**需求** {_card(evidence) or '-'}",
        f"**项目** {_safe(_evidence(evidence, 'project')) or '-'}",
        f"**协作群** {_safe(_evidence(evidence, 'group_name')) or '-'}",
        f"**截止** {_moment(gateway_request['deadline_at'])}",
    ]
    gate = _evidence(evidence, "gate")
    gate = gate if isinstance(gate, dict) else {}
    subject = _line(gate.get("subject"))
    effect = _line(gate.get("effect"))
    if subject:
        lines += ["", f"**本次审批** {subject}"]
    if effect:
        lines += [f"**批准后** {effect}"]
    summary = [_line(item) for item in gate.get("summary") or []]
    summary = [item for item in summary if item]
    if summary:
        lines += ["", "**内容摘要**", *(f"- {item}" for item in summary)]
    documents = _documents(_evidence(evidence, "documents"))
    if documents:
        lines += ["", "**需求文档**", *(f"- {document}" for document in documents)]
    lines += [
        "",
        "**同意请回复**",
        f"APPROVE {approval_id}",
        "",
        "**驳回请回复**",
        f"REJECT {approval_id}",
        "",
        "> 回复必须带 approval_id；它绑定本次内容，需求一变即失效。",
        f"> input_hash `{input_hash[:12]}…`",
        f"> run_id `{run_id[:12]}…`",
    ]
    content_hash = _line(gate.get("content_hash"))
    if content_hash:
        lines.append(f"> 产物哈希 `{content_hash[:12]}…`")
    return "\n".join(lines)


def _card(evidence: Any) -> str:
    card_id = _safe(_evidence(evidence, "card_id"))
    title = _safe(_evidence(evidence, "card_title"))
    url = _url(_evidence(evidence, "card_url"))
    label = _link_label(card_id)
    head = f"[{label}]({url})" if label and url else card_id
    return " ".join(part for part in (head, title) if part)


def _documents(value: Any) -> list[str]:
    """Render only well-formed links; a broken one is worse than a missing one."""
    if not isinstance(value, list):
        return []
    rendered = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _url(item.get("url"))
        label = _link_label(item.get("label")) or url
        if url:
            rendered.append(f"[{label}]({url})")
    return rendered


def _link_label(value: Any) -> str:
    """Infoflow renders a link label containing whitespace twice, so never emit one."""
    text = _safe(value)
    for character in "[]()":
        text = text.replace(character, "")
    return "-".join(text.split())


def _url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate.startswith(("http://", "https://")) or any(
        char.isspace() for char in candidate
    ):
        return ""
    return candidate[:400]


def _moment(value: Any) -> str:
    try:
        return _deadline(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def _evidence(evidence: Any, field: str) -> Any:
    return evidence.get(field, "") if isinstance(evidence, dict) else ""


def _safe(value: Any) -> str:
    return str(value).replace("@", "[at]")[:200]


def _line(value: Any) -> str:
    """Only real text becomes a line; a missing field must not render as "None"."""
    return _safe(value) if isinstance(value, str) and value.strip() else ""
