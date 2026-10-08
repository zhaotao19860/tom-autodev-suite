from __future__ import annotations

from execution_guard import execution_guard, migration_exempt

import hashlib
import inspect
import uuid
from datetime import datetime, timezone
from typing import Any

from approval_contract import ApprovalGatewayResult, parse_gateway_result
from collaboration import group_id_for_run
import workflow_spec
from approval_ledger import gate_of
from run_change import FEEDBACK_PLACEHOLDER
from approval_summary import gate_intent, references


_RECORD_KEY = "approval.gateway.infoflow"
_REPLY_KEY = "approval.gateway.infoflow.reply"

def correct_approval_message(orchestrator, run_id, approval_id, input_hash, *, actor, reason, client):
    """Append a presentation correction, never replace a gate or its old receipt."""
    from approval_summary import _hash, pinned_context, _is_diagnosis
    from run_change import superseded
    blocked = execution_guard(orchestrator.state, run_id)
    if blocked:
        return blocked
    def refuse(code, message):
        return {"ok": False, "reason_code": code, "message": message}
    approval = orchestrator.approvals.get(approval_id)
    if not approval or approval.get("run_id") != run_id or approval.get("input_hash") != input_hash:
        return refuse("APPROVAL_BINDING_MISMATCH", "审批编号、运行或输入哈希不匹配，未发送纠正消息。")
    if not actor or not reason or actor not in sum(approval["member_policy"].values(), []):
        return refuse("CORRECTION_ACTOR_REQUIRED", "请由审批成员提供操作者身份和纠正原因。")
    if (approval.get("status") != "PENDING" or approval.get("effective_decision")
            or _deadline(approval["deadline_at"]) <= datetime.now(timezone.utc)
            or superseded(orchestrator.state, run_id, approval)):
        return refuse("APPROVAL_NOT_PENDING", "审批已处理、过期或失效；中文纠正不能重开审批或延长截止时间。")
    if gate_of(approval["action"]) != "G6":
        return refuse("CORRECTION_GATE_UNSUPPORTED", "此纠正入口仅支持诊断审批。")
    if orchestrator.state.pending_intents(run_id):
        # Existing correction below is safe to replay only with its durable receipt.
        pending = orchestrator.state.pending_intents(run_id)
        if any(p.get("operation") != "approval.message-correction" for p in pending):
            return refuse("QUERY_REQUIRED", "存在结果未确认的操作，须先核对原投递记录，不能盲目重发。")
    request_id = _request_id(approval)
    original = orchestrator.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
    if not original or not original.get("content"):
        return refuse("QUERY_REQUIRED", "缺少原消息送达凭据，须先核对原投递。")
    detail = pinned_context(orchestrator, run_id, "G6", input_hash)
    material = detail.get("_review_content") or {}
    if detail.get("material_status") != "PINNED" or not _is_diagnosis(material.get("draft") or {}):
        return refuse("APPROVAL_CONTENT_MISMATCH", "未取得与原审批哈希一致的诊断草案，未发送纠正消息。")
    request = {**approval, "evidence": {"gate": detail}}
    recipients = approval["member_policy"]["infoflow"]
    content = ("## 诊断审批中文说明纠正\n\n"
               "本消息仅纠正原审批展示，不创建新审批、不改变审批内容或截止时间；"
               "仍请使用原审批编号回复。原消息保留供审计。\n\n"
               + _approval_markdown(request, recipients, mention=True))
    # One correction per exact presentation. Claim before send; unknown sends
    # require reconciliation, not abandonment or a second ordinary approval.
    key = f"approval.message-correction:{approval_id}:{_hash(content)}"
    saved = orchestrator.state.result_by_idempotency_key(key)
    if saved:
        return {"ok": True, "reason_code": "CORRECTION_ALREADY_DELIVERED",
                "approval_id": approval_id, "input_hash": input_hash, "content": content}
    payload = {"approval_id": approval_id, "input_hash": input_hash,
               "deadline_at": approval["deadline_at"], "actor": actor, "reason": reason,
               "original_content_hash": _hash(original["content"]),
               "content_hash": _hash(content), "content": content}
    claim = orchestrator.state.claim_intent(run_id, "approval.message-correction", key, payload)
    if claim["status"] != "CLAIMED":
        return refuse("QUERY_REQUIRED", "纠正消息已有投递意图但结果未确认；禁止重复发送，请核对该意图。")
    try:
        receipt = deliver_markdown(client, orchestrator.state, run_id, recipients, lambda _: content)
        if not isinstance(receipt, dict) or not receipt.get("message_key"):
            raise ValueError("CORRECTION_RECEIPT_MISSING")
        orchestrator.state.receipt(claim["intent"]["intent_id"], {
            **payload, "receipt": receipt, "channel": "infoflow",
        }, [])
    except Exception as error:
        if getattr(error, "outcome", None) == "REJECTED_BEFORE_SEND" or isinstance(error, TypeError):
            orchestrator.state.withdraw_intent(run_id, "approval.message-correction", key, payload)
            return refuse("CORRECTION_REJECTED", "网关在发送前拒绝了纠正消息，未送达；已撤回投递意图，可修正后重试。")
        return refuse("QUERY_REQUIRED", "纠正消息发送结果未确认，保留投递意图；请核对，勿重复发送。")
    # The correction repeats "同意请回复 …"; give it buttons right under it.
    try:
        buttons = _deliver_approval_card(
            client, orchestrator.state, request, presentation=f"fix-{_hash(content)[:12]}",
        )
    except Exception as error:  # noqa: BLE001 - typed replies still work
        buttons = {"status": "UNKNOWN", "reason_code": type(error).__name__}
    return {"ok": True, "reason_code": "CORRECTION_DELIVERED",
            "approval_id": approval_id, "input_hash": input_hash,
            "deadline_at": approval["deadline_at"], "content": content, "receipt": receipt,
            "buttons": buttons.get("status")}


def corrected_content(state_store: Any, approval: dict[str, Any]) -> str | None:
    """The latest delivered correction of a pending approval's message, if any."""
    latest = state_store.latest_response(
        approval.get("run_id"), "approval.message-correction",
        f"approval.message-correction:{approval.get('approval_id')}:",
    )
    content = latest.get("content") if isinstance(latest, dict) else None
    return content if isinstance(content, str) and content else None


def _delivery_guard(state: Any, run_id: Any, action: Any) -> dict[str, Any] | None:
    """Guard delivery, except for the gate that authorizes the migration itself.

    Opening and answering the migration gate is the one write allowed while the old
    workflow pin is drifted (see `execution_guard.guard_execution`). The controller
    exempts it, so this boundary must too: guarding it here suppressed the card
    without sending anything, the ledger then refused every decision for a gate it
    considered undelivered, and the paused run could never be migrated at all.
    """
    if migration_exempt(action):
        return None
    return execution_guard(state, run_id)


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


def _review_gate(request: dict[str, Any]) -> dict[str, Any]:
    evidence = request.get("evidence") or {}
    gate = {**gate_intent(request.get("action")), **(evidence.get("gate") or {})}
    # Retry evidence contains the originally persisted English field summary.
    # Rebuild presentation from that exact draft, not a latest artifact, while
    # preserving all canonical material and approval identities.
    from approval_summary import _is_diagnosis, _diagnosis_summary
    material = gate.get("_review_content")
    draft = material.get("draft") if isinstance(material, dict) else None
    if isinstance(draft, dict) and _is_diagnosis(draft):
        gate["summary"] = _diagnosis_summary(draft)
        gate["checklist"] = [
            "失败证据、根因、修复范围、预算与回退路径",
            "失败证据包、诊断结论、修复范围、拟议改动与证据引用",
        ]
        gate["effect"] = (
            "按诊断路线返回任务计划、规格说明、架构复核或停止；"
            "后续代码改动与提交审批仍须分别通过"
        )
    if not gate.get("references"):
        gate["references"] = references({
            key: value for key, value in evidence.items()
            if key not in {"gate", "documents", "progress"}
        })
    return gate


_MATERIAL_LABELS = ("本次审批材料包", "本次审批可读预览", "已保存草案")


def _reading_lines(gate: dict[str, Any]) -> list[str]:
    lines = []
    for item in gate.get("references") or []:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            continue
        label = _link_label(item.get("label") or "材料")
        ref = item["ref"]
        url = _url(ref)
        if url:
            lines.append(f"[{label}]({url})")
        else:
            # Never truncate a local path/hash into a locator that cannot be opened.
            ref = " ".join(ref.split()).replace("`", "")
            lines.append(f"{label}：`{ref}`")
    return list(dict.fromkeys(lines))


def _deliver_approval_card(
    client: Any, state_store: Any, gateway_request: dict[str, Any],
    *, presentation: str | None = None,
) -> Any:
    """Send to the collaboration group, or each authorized private recipient.

    Persist each target's outcome separately. An exception can mean remote delivery
    is unknown; a replay must not resend successful OR uncertain targets blindly.
    Markdown remains the reply fallback, and degraded button delivery is explicit.
    ``presentation`` names a follow-up card for an already delivered approval, so a
    reminder that repeats the reply instructions also carries its own buttons.
    """
    group_id = group_id_for_run(state_store, gateway_request["run_id"])
    if not hasattr(client, "send_approval_card"):
        return {"status": "UNAVAILABLE", "reason_code": "APPROVAL_BUTTON_UNSUPPORTED"}
    evidence = gateway_request.get("evidence")
    gate = _review_gate(gateway_request)
    action = str(gateway_request.get("action") or "gate")
    reads = _reading_lines(gate)
    lines = [
        f"需求 {_safe(_evidence(evidence, 'card_id'))} {_safe(_evidence(evidence, 'card_title'))}".strip(),
        f"本次审批 {_line(gate.get('subject'))}" if _line(gate.get("subject")) else "",
        "审批重点 " + "；".join(gate.get("checklist") or []),
        ("材料缺口：未取得与本次 hash 匹配的产物，请勿批准"
         if gate.get("material_status") == "MISSING"
         else "请先查看 " + ("；".join(reads[:2]) if reads else "材料未齐，请先在 IDE 补齐，勿盲批")),
        f"批准后 {_line(gate.get('effect'))}" if _line(gate.get("effect")) else "",
        ("按钮含义：同意=继续 run；需修改=保留 run、按意见重做并重发；停止run=停止本 run。"
         if gate_of(action) in _REVISABLE_GATES
         else "按钮含义：同意=执行本操作；停止run=停止本 run。"),
        f"版本 {gateway_request['input_hash'][:12]}…；截止 {_moment(gateway_request['deadline_at'])}；完整材料及本地文件位置见同一审批详情",
    ]
    targets = [("group", group_id)] if group_id is not None else [
        ("user", member) for member in sorted({
            _uuap(member) for member in gateway_request["member_policy"]["infoflow"]
        })
    ]
    outcomes = []
    for target_type, target_id in targets:
        key = f"approval.button:{_request_id(gateway_request)}:{target_type}:{target_id}"
        if presentation:
            key += f":{presentation}"
        saved = state_store.idempotency_result(key)
        if saved is None:
            try:
                state_store.save_idempotency_result(key + ":intent", {"attempt": uuid.uuid4().hex})
            except ValueError:
                # Another sender may have completed between our read and claim.
                saved = state_store.idempotency_result(key) or {
                    "status": "UNKNOWN", "reason_code": "QUERY_REQUIRED",
                }
                outcomes.append({"target_type": target_type, "target_id": target_id, **saved})
                continue
            try:
                receipt = client.send_approval_card(
                    approval_id=gateway_request["approval_id"],
                    target_type=target_type, target_id=target_id,
                    title=f"tom-autodev {action} 审批",
                    question=("待你审批：请先核对材料和重点，再选择「同意」「需修改」或「停止run」。"
                              if gate_of(action) in _REVISABLE_GATES
                              else "待你审批：请先核对材料和重点，再选择「同意」或「停止run」。"),
                    lines=[line for line in lines if line],
                    revisable=gate_of(action) in _REVISABLE_GATES,
                    # Who may answer, so a shared group card only reads as answered
                    # when the tap came from an approver. The ledger still decides.
                    approvers=sorted({
                        _uuap(member)
                        for member in gateway_request["member_policy"]["infoflow"]
                    }),
                    **({"presentation": presentation} if presentation else {}),
                )
                if (not isinstance(receipt, dict)
                        or not isinstance(receipt.get("card_id"), str)
                        or not receipt["card_id"].strip()):
                    raise ValueError("CARD_RESPONSE_INVALID")
                saved = {"status": "DELIVERED", "receipt": receipt}
            except Exception as error:  # remote outcome may be unknown; do not replay
                saved = {"status": "UNKNOWN", "reason_code": type(error).__name__}
            saved = state_store.save_idempotency_result(key, saved)
        outcomes.append({"target_type": target_type, "target_id": target_id, **saved})
    return {
        "status": "DELIVERED" if all(x["status"] == "DELIVERED" for x in outcomes) else "DEGRADED",
        "targets": outcomes,
    }


def _uuap(recipient: str) -> str:
    return recipient.split("@", 1)[0] if isinstance(recipient, str) else ""


class ComateApprovalClient:
    """The comate channel is this CLI session, so delivery is the printed request."""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval = payload.get("approval") if isinstance(payload, dict) else None
        if not isinstance(approval, dict):
            raise ValueError("APPROVAL_REQUEST_INVALID")
        receipt = {
            "channel": "comate",
            "approval_id": approval.get("approval_id"),
            "run_id": approval.get("run_id"),
            "input_hash": approval.get("input_hash"),
            "deadline_at": approval.get("deadline_at"),
            "surface": "comate-cli",
        }
        progress = payload.get("progress")
        if isinstance(progress, dict):
            from progress_snapshot import render as render_progress

            receipt["progress"] = progress
            receipt["progress_text"] = render_progress(progress)
        return receipt


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
        progress_provider: Any | None = None,
    ):
        self.state = state_store
        self.notify_client = notify_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reply_consumer = reply_consumer
        self.progress_provider = progress_provider

    def request(self, gateway_request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(gateway_request, dict):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        blocked = _delivery_guard(
            self.state, gateway_request.get("run_id"), gateway_request.get("action")
        )
        if blocked is not None:
            return blocked
        required = ("run_id", "approval_id", "input_hash", "deadline_at", "member_policy")
        if any(not gateway_request.get(field) for field in required):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        if gateway_request.get("channel") != "infoflow":
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        recipients = gateway_request["member_policy"].get("infoflow")
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        if any(not isinstance(member, str) or not _uuap(member).strip() for member in recipients):
            raise ValueError("APPROVAL_ENVELOPE_INVALID")
        deadline_at = _deadline(gateway_request["deadline_at"]).isoformat()

        request_id = _request_id(gateway_request)
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is not None:
            return self._resolved(stored["record"], gateway_request.get("action"))
        attempt_key = gateway_request.get("_delivery_attempt_key")
        intent_key = (
            f"{attempt_key}:transport"
            if isinstance(attempt_key, str) and attempt_key
            else f"approval.delivery:{request_id}:intent"
        )
        if self.state.idempotency_result(intent_key) is None:
            # Prove missing methods/non-callables/signature mismatches locally,
            # before a permanent claim can contradict the caller's retry_allowed.
            # Never infer "not sent" from an exception raised INSIDE the method.
            group_id = group_id_for_run(self.state, gateway_request["run_id"])
            if group_id is None:
                sender = self.notify_client.send_markdown
                args = (sorted(recipients), "")
            else:
                sender = self.notify_client.send_group_markdown
                args = (group_id, "", [_uuap(item) for item in sorted(recipients)])
            if not callable(sender):
                raise TypeError("markdown sender is not callable")
            try:
                signature = inspect.signature(sender)
            except (TypeError, ValueError):
                # Some extension callables expose no signature. Keep the normal
                # claim-before-call protection rather than guessing dispatch.
                pass
            else:
                signature.bind(*args)
        # The outer controller normally serializes a run, but this transport is
        # also called directly. Claim BEFORE the first remote side effect, not
        # merely before the buttons. A crash/partial markdown send without a final
        # receipt is unknown and requires reconciliation, never a blind retry.
        try:
            self.state.save_idempotency_result(
                intent_key, {"attempt": uuid.uuid4().hex}
            )
        except ValueError:
            stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
            if stored is not None:
                return self._resolved(stored["record"], gateway_request.get("action"))
            raise ValueError("APPROVAL_DELIVERY_QUERY_REQUIRED") from None
        delivered_content: list[str] = []

        progress = None
        if callable(self.progress_provider):
            try:
                progress = self.progress_provider(gateway_request["run_id"])
            except Exception:  # noqa: BLE001 - approval delivery remains best effort
                progress = None

        def render(mention: bool) -> str:
            card = _approval_markdown(
                gateway_request, sorted(recipients), mention=mention, progress=progress
            )
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
            "deadline_at": deadline_at,
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
                # Outside the gateway record, whose field set is fixed by
                # `parse_gateway_result`. Re-reading the request needs the gate name to
                # know whether it is the migration gate, which stays answerable while
                # the run's old workflow pin is drifted.
                "action": gateway_request.get("action"),
                # Kept so a reminder repeats the exact card the approvers already saw,
                # and so a timed-out gate can be reissued without rebuilding evidence.
                "content": content,
                "evidence": gateway_request.get("evidence") or {},
                # Best effort: a gate that only got the markdown is still answerable by
                # typing, so a missing card must never look like a failed delivery.
                "card": card,
            },
        )
        if card.get("status") != "DELIVERED":
            warning = (
                f"## tom-autodev 审批操作提示（不是新审批）\n\n"
                f"审批 {gateway_request['approval_id']} 的按钮未完整送达或结果未确认。"
                "\n请先阅读审批详情；可回复“同意”“需修改”或“停止run”，也可以在 IDE 处理。"
                "\n这不表示审批已通过；不会自动重复发送结果不明的按钮。"
            )
            try:
                delivered_warning = deliver_markdown(
                    self.notify_client, self.state, gateway_request["run_id"],
                    sorted(recipients), lambda _: warning,
                )
            except Exception as error:
                delivered_warning = {"status": "UNKNOWN", "reason_code": type(error).__name__}
            self.state.save_idempotency_result(
                f"approval.button.warning:{request_id}", delivered_warning
            )
        return self._resolved(record, gateway_request.get("action"))

    def delivered(self, request_id: str) -> dict[str, Any] | None:
        """The card and evidence a request was delivered with, when they were stored."""
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is None:
            return None
        evidence = stored.get("evidence")
        return {
            "content": stored.get("content") or "",
            "evidence": evidence if isinstance(evidence, dict) else {},
            "card": stored.get("card"),
        }

    def reconcile(self, gateway_request: dict[str, Any]) -> dict[str, Any] | None:
        """Report an already-delivered request so a failed send is never replayed blind."""
        if not isinstance(gateway_request, dict) or any(
            not gateway_request.get(field) for field in ("run_id", "approval_id", "input_hash")
        ):
            return None
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{_request_id(gateway_request)}")
        if stored is None:
            return None
        # The persisted action wins: the guard exemption must follow what this
        # request was actually delivered as, never what a caller now claims.
        return self._resolved(stored["record"], stored.get("action") or gateway_request.get("action"))

    def wait(self, request_id: str, timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds  # A caller's wait bound must never move the approved deadline.
        stored = self.state.idempotency_result(f"{_RECORD_KEY}:{request_id}")
        if stored is None:
            raise ValueError("APPROVAL_WAIT_RESPONSE_INVALID")
        return self._resolved(stored["record"], stored.get("action"))

    def _resolved(self, record: dict[str, Any], action: Any = None) -> ApprovalGatewayResult:
        # Re-reading a delivered card can persist its new reply or timeout
        # handoff. Treat request/reconcile/wait as execution at this boundary.
        blocked = _delivery_guard(self.state, record.get("run_id"), action)
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
    gateway_request: dict[str, Any], recipients: list[str], *, mention: bool = False,
    progress: dict[str, Any] | None = None,
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
    gate = _review_gate(gateway_request)
    subject = _line(gate.get("subject"))
    effect = _line(gate.get("effect"))
    if subject:
        lines += ["", f"**本次审批** {subject}"]
    if effect:
        lines += [f"**批准后** {effect}"]
    lines += ["", "**审批重点（请逐项核对）**",
              "", *(f"- {_line(item)}" for item in gate.get("checklist") or []),
              "", "**请先查看这些文件 / 证据**", ""]
    body = _review_body(gate) if gate.get("material_status") != "MISSING" else []
    reads = _reading_lines(gate)
    if body:
        # Every card keeps the readable content in 「审批正文」; the reading list then
        # only points at the material pack, never a wall of raw artifact ids/hashes that
        # just repeat what the body and pack already carry. This applies to all gates.
        lines.append("- 完整内容见下方「审批正文」，其余固定版本与证据标识见材料包。")
        lines += [f"- {item}" for item in reads if item.startswith(_MATERIAL_LABELS)]
    elif reads:
        lines += [f"- {item}" for item in reads]
    else:
        lines.append("- 未提供本次固定版本材料的位置，请先在 IDE 补齐；需求背景链接不能替代审批产物。")
    if gate.get("material_status") == "MISSING":
        lines += ["", "**材料缺口：未取得与本次 hash 匹配的产物，请勿批准。**"]
    lines += ["", f"**决定边界** {_line(gate.get('decision'))}"]
    summary = [_line(item) for item in gate.get("summary") or []]
    summary = [item for item in summary if item]
    if summary and not (body and all(f"- {item}" in body for item in summary)):
        lines += ["", "**内容摘要**", *(f"- {item}" for item in summary)]
    documents = _documents(_evidence(evidence, "documents"))
    if documents:
        lines += ["", "**需求文档**", *(f"- {document}" for document in documents)]
    revision = _revision_lines(gate) if body else []
    if revision:
        lines += ["", *revision]
    if body:
        lines += ["", "**审批正文（本次 input_hash 绑定的内容）**", "", *body]
    if progress:
        from progress_snapshot import render as render_progress

        approval_progress = {**progress, "next_action": {"owner": "你", "text": f"核对上述材料，处理 {action} 审批"}}
        lines += ["", "**整体进度（辅助信息）**", render_progress(approval_progress)]
    lines += [
        "",
        "**同意请回复**",
        f"同意 {approval_id}",
        "",
        *(
            [
                "**需修改请回复**",
                f"需修改 {approval_id} <{FEEDBACK_PLACEHOLDER}>",
                "",
            ]
            if gate_of(action) in _REVISABLE_GATES
            else []
        ),
        "**停止 run 请回复**",
        f"停止 {approval_id}",
        "",
        *(["> 点卡片“需修改”后直接在本群回复修改意见即可（不用带审批 ID）；也可按上面格式一次回复。"
           "也可以在 Comate 里直接说“/tom-autodev <卡片> 需修改 <意见>”，一步提意见并继续。"
           "“停止”则结束本 run。收到意见后自动登记重做，在 IDE 继续即生成新版草案并重新发起本门。"
           "历史“驳回”“REJECT”等回复仍兼容。"]
          if gate_of(action) in _REVISABLE_GATES else []),
        "> 文字回复的同意/停止/需修改必须带 approval_id；它绑定本次内容，需求一变即失效。",
        f"> input_hash `{input_hash[:12]}…`",
        f"> run_id `{run_id[:12]}…`",
    ]
    content_hash = _line(gate.get("content_hash"))
    if content_hash:
        lines.append(f"> 产物哈希 `{content_hash[:12]}…`")
    return "\n".join(lines)


_REVIEW_BODY_LIMIT = 12000


_REVISABLE_GATES = workflow_spec.REVISABLE_GATES
_DIFF_LIMIT = 4000


def _review_body(gate: dict[str, Any]) -> list[str]:
    """The draft under review, readable in the chat itself.

    The material packet is a local file on the controller host; a reviewer in
    Infoflow cannot open it, so the reviewed content has to travel with the message.
    """
    material = gate.get("_review_content")
    draft = material.get("draft") if isinstance(material, dict) else None
    if not isinstance(draft, dict):
        return []
    lines = _draft_lines(draft)
    # One level below the message title so the body never reads as a new message.
    lines = [f"#{line}" if line.startswith("#") else line for line in lines]
    kept, used = [], 0
    for line in lines:
        used += len(line) + 1
        if used > _REVIEW_BODY_LIMIT:
            kept += ["", "> 正文超过消息篇幅上限，以上为前半部分；完整内容以上面的材料包为准，请在 IDE 中核对后再决定。"]
            break
        kept.append(line)
    return kept


def _revision_lines(gate: dict[str, Any]) -> list[str]:
    """Why this is a new version, and what moved since the one that was sent back."""
    import difflib

    material = gate.get("_review_content")
    material = material if isinstance(material, dict) else {}
    reentry = material.get("reentry")
    if not isinstance(reentry, dict):
        return []
    kind = {
        "REVISE": "需修改后按意见重做",
        "DIAGNOSIS": "按已批准诊断的修复方向重做",
    }.get(reentry.get("kind"), "变更请求回退后重做")
    note = reentry.get("feedback") or reentry.get("reason")
    lines = ["**本次修改说明**", f"- {kind}（发起人 {_line(reentry.get('actor')) or '-'}）"]
    if isinstance(note, str) and note.strip():
        text = " ".join(note.split()).replace("@", "[at]")
        lines.append(f"- 意见：{text[:1000]}{'…' if len(text) > 1000 else ''}")
    steps = [step for step in reentry.get("repair_plan") or [] if isinstance(step, str) and step.strip()]
    if steps:
        text = "；".join(" ".join(step.split()) for step in steps).replace("@", "[at]")
        lines.append(f"- 修复计划：{text[:1000]}{'…' if len(text) > 1000 else ''}")
    previous = material.get("previous_draft")
    draft = material.get("draft")
    if not isinstance(previous, dict) or not isinstance(draft, dict):
        return lines
    diff = list(difflib.unified_diff(
        _draft_lines(previous), _draft_lines(draft), "上一版", "本版", n=1, lineterm="",
    ))[2:]
    if not diff:
        return [*lines, "- 与上一版内容相同"]
    fence = "`" * 3
    kept, used = [], 0
    for line in diff:
        used += len(line) + 1
        if used > _DIFF_LIMIT:
            kept.append("… 差异过长，其余部分请对照下方正文")
            break
        kept.append(line.replace(fence, "'" * 3))
    return [*lines, "", "**与上一版的差异**", "", fence + "diff", *kept, fence]


def _draft_lines(draft: dict[str, Any]) -> list[str]:
    from phase_document import content_lines
    from approval_summary import _diagnosis_summary, _diagnosis_text, _is_diagnosis

    if isinstance(draft.get("spec"), dict):
        lines = content_lines(draft["spec"], "SPEC")
        if isinstance(draft.get("dag"), dict):
            lines += ["", "## 关联 DAG（任务覆盖，随 Spec 一并核对，不单独审批）", "",
                      *content_lines(draft["dag"], "TASKS")]
    elif _is_diagnosis(draft):
        lines = ["## 诊断摘要", ""]
        lines += [f"- {item}" for item in _diagnosis_summary(draft)]
        for key, label in (("hypothesis", "根因假设"), ("repair_direction", "修复方向")):
            if draft.get(key) not in (None, ""):
                lines += ["", f"### {label}", "", f"- {_diagnosis_text(draft[key])}"]
        for key, label in (("repair_plan", "修复计划"), ("comparison", "对比证据")):
            values = draft.get(key)
            if isinstance(values, list) and values:
                lines += ["", f"### {label}", ""]
                lines += [f"- {_diagnosis_text(item)}" for item in values]
        supporting = [
            f"{label} {len(draft[key])} 条"
            for key, label in (("log_evidence", "日志证据"), ("reproduction", "复现步骤"),
                               ("minimal_verification", "最小验证"))
            if isinstance(draft.get(key), list) and draft[key]
        ]
        if supporting:
            lines += ["", f"- 另有{'、'.join(supporting)}，原文见材料包"]
    else:
        lines = content_lines(draft, "TASKS" if {"nodes", "edges"} <= set(draft) else None)
    return lines


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
