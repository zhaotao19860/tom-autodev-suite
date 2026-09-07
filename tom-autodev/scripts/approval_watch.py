from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from approval_contract import STRICT_APPROVAL_TIMEOUT_SECONDS
from approval_delivery import deliver_markdown, mention_line


_ACK_KEY = "approval.ack"
_NUDGE_KEY = "approval.nudge"
_REISSUE_KEY = "approval.reissue"
_RESUME_KEY = "resume-notice"
_IDE_TURN_KEY = "ide-turn-notice"
# Reminders get more urgent as the deadline approaches; the bucket index keeps each
# reminder idempotent without adding state of its own.
_NUDGE_BUCKETS = ((1800, 3), (7200, 2), (21600, 1))
# A finished run has nothing to report; keep this in step with `transition_policy`.
_TERMINAL_STATES = frozenset({"RELEASE_SUCCESS", "STOPPED"})


class ApprovalWatcher:
    """Land journalled 如流 decisions without anyone poking the CLI.

    `await-approval` only reads the journal while a command happens to run, so a
    decision typed into 如流 sat there silently until the next invocation. This
    watcher closes that gap: it polls the open approvals, lets the transport apply
    any bound reply, and answers the approver in 如流 so the reply is visibly
    received. It never advances a phase — phase work needs the agent, and a
    background process must not decide engineering on its own.

    Inbound replies only exist while the WebSocket is connected, so each round also
    revives a dead gateway: that bounds the window in which a reply can be lost to
    one interval instead of the hours until someone notices. What still slips through
    is repeated: an undecided gate is reminded as its deadline approaches, and a gate
    that times out anyway is reissued once against the same `input_hash`, because a
    timed-out approval is terminal in the ledger and cannot be revived in place.
    """

    def __init__(
        self,
        orchestrator: Any,
        client_factory: Any,
        notify_client: Any,
        *,
        sleeper: Any | None = None,
        reporter: Any | None = None,
        clock: Any | None = None,
        reissue: bool = True,
    ):
        self.orchestrator = orchestrator
        self.client_factory = client_factory
        self.notify_client = notify_client
        self.sleeper = sleeper or time.sleep
        self.reporter = reporter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reissue = reissue

    def tick(self) -> list[dict[str, Any]]:
        outcomes = []
        channel = self._ensure_channel()
        if channel is not None:
            outcomes.append(channel)
            if self.reporter is not None:
                self.reporter(channel)
        for approval in self.orchestrator.approvals.pending():
            outcome = self._settle(approval)
            if outcome is None:
                continue
            outcomes.append(outcome)
            if self.reporter is not None:
                # A watcher that only reports at exit is useless while it runs.
                self.reporter(outcome)
        for progress in self._progress():
            outcomes.append(progress)
            if self.reporter is not None:
                self.reporter(progress)
        for handoff in self._handoff():
            outcomes.append(handoff)
            if self.reporter is not None:
                self.reporter(handoff)
        return outcomes

    def _handoff(self) -> list[dict[str, Any]]:
        """Say in the group when the ball is back with the operator.

        A gate is answered in 如流, but landing the phase needs the agent, which only
        runs when someone types 继续 in the IDE. Nothing changes state in between, so
        `_progress` — which fires on a state change and stays quiet while a gate is
        open — never reports this moment, and the run looks alive while it is simply
        waiting. The notice goes to the group that carried the gate, because that is
        where the approver already is.
        """
        from run_brief import build

        notices = []
        for latest in self.orchestrator.state.latest_states():
            if latest["state"] in _TERMINAL_STATES:
                continue
            run_id = latest["run_id"]
            brief = build(self.orchestrator, run_id)
            if brief["waiting_on"] or brief["in_flight"] or brief.get("blocked"):
                continue
            if brief["next"]["owner"] != "Comate":
                continue
            approval = self._approved_gate(run_id, brief.get("gate"), latest.get("created_at"))
            if approval is None:
                continue
            key = f"{_RESUME_KEY}:{run_id}:{latest['event_id']}:{approval['approval_id']}"
            if self.orchestrator.state.idempotency_result(key) is not None:
                continue
            recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
            if not recipients:
                continue
            try:
                receipt = deliver_markdown(
                    self.notify_client,
                    self.orchestrator.state,
                    run_id,
                    recipients,
                    lambda mention: _resume_markdown(
                        brief, approval, mention and recipients
                    ),
                )
            except Exception as error:  # noqa: BLE001 - any failure here is operational
                notices.append({"reason_code": "RESUME_NOTICE_FAILED", "detail": str(error)})
                continue
            self.orchestrator.state.save_idempotency_result(key, {"receipt": receipt})
            notices.append(
                {
                    "reason_code": "RESUME_NOTICE_SENT",
                    "run_id": run_id,
                    "state": latest["state"],
                    # Deliberately not `approval_id`: this outcome reports a position,
                    # not the settlement of a gate, and a reporter must be able to
                    # tell the two apart.
                    "gate_approval_id": approval["approval_id"],
                }
            )
        return notices

    def _approved_gate(self, run_id: str, gate: Any, since: Any) -> dict[str, Any] | None:
        """The gate this phase needs, approved after the phase was entered.

        Matching on the gate the current phase requires is what keeps the notice from
        repeating after the phase lands: the next phase asks for a different gate,
        which has no decision yet. The decision also has to be newer than the state
        event, or a run that re-enters a phase would be told to continue on the
        strength of the approval that the re-entry just discarded.
        """
        if not isinstance(gate, str) or not gate:
            return None
        for row in reversed(self.orchestrator.approvals.for_run(run_id)):
            if row.get("action") != gate or row.get("effective_decision") != "APPROVE":
                continue
            if _later(row.get("resolved_at"), since):
                return row
        return None

    def _progress(self) -> list[dict[str, Any]]:
        """Push a run's position to 如流 once per state change.

        Without this, a phase landing is invisible until someone opens the CLI, so
        the operator cannot tell whether anything is in flight or whose turn it is.
        The notice is keyed by the event it reports, and a run seen for the first
        time is recorded silently: a restarted watcher must not replay the position
        of every run it has never reported on.
        """
        from run_brief import build, render

        notices = []
        for latest in self.orchestrator.state.latest_states():
            if latest["state"] in _TERMINAL_STATES:
                continue
            run_id = latest["run_id"]
            key = f"progress-notice:{run_id}:{latest['event_id']}"
            if self.orchestrator.state.idempotency_result(key) is not None:
                continue
            if not self._reported_before(run_id, latest["event_id"]):
                self.orchestrator.state.save_idempotency_result(key, {"seeded": latest["state"]})
                continue
            brief = build(self.orchestrator, run_id)
            if brief["waiting_on"]:
                # The gate card already says what to do; a second message competes with it.
                self.orchestrator.state.save_idempotency_result(key, {"skipped": "gate_open"})
                continue
            recipients = self._recipients(run_id)
            if not recipients:
                continue
            try:
                # Position goes to the owner privately; the group carries the gates,
                # and a per-phase status line for everyone would drown them.
                self.notify_client.send_markdown(recipients, render(brief))
            except Exception as error:  # noqa: BLE001 - any failure here is operational
                notices.append({"reason_code": "PROGRESS_NOTICE_FAILED", "detail": str(error)})
                continue
            self.orchestrator.state.save_idempotency_result(
                key, {"run_id": run_id, "state": latest["state"]}
            )
            notices.append(
                {
                    "reason_code": "PROGRESS_NOTICE_SENT",
                    "run_id": run_id,
                    "state": latest["state"],
                }
            )
        return notices

    def _reported_before(self, run_id: str, event_id: str) -> bool:
        return any(
            self.orchestrator.state.idempotency_result(
                f"progress-notice:{run_id}:{event['event_id']}"
            )
            is not None
            for event in self.orchestrator.state.events(run_id)
            if event["event_id"] != event_id
        )

    def _recipients(self, run_id: str) -> list[str]:
        """The owner gets the position; a gate is what the whole group is asked about.

        Sending every phase landing to every role would train people to ignore the
        channel that also carries the approvals.
        """
        events = self.orchestrator.state.events(run_id)
        payload = events[0].get("payload") if events else None
        binding = payload.get("collaboration_binding") if isinstance(payload, dict) else None
        owner = binding.get("owner") if isinstance(binding, dict) else None
        if isinstance(owner, str) and owner:
            return [owner]
        for row in reversed(self.orchestrator.approvals.for_run(run_id)):
            policy = row.get("member_policy")
            members = policy.get("infoflow") if isinstance(policy, dict) else None
            if isinstance(members, list) and members:
                return sorted(members)
        return []

    def _ensure_channel(self) -> dict[str, Any] | None:
        """Restart a dead gateway before reading replies, and report a failure once."""
        if not hasattr(self.notify_client, "ensure_ready"):
            return None
        try:
            self.notify_client.ensure_ready()
        except Exception as error:  # noqa: BLE001 - any failure here is operational
            return {"reason_code": "INFOFLOW_GATEWAY_UNAVAILABLE", "detail": str(error)}
        return None

    def run(self, interval_seconds: float = 10, iterations: int | None = None) -> list[dict[str, Any]]:
        settled = []
        rounds = 0
        while iterations is None or rounds < iterations:
            settled += self.tick()
            rounds += 1
            if iterations is not None and rounds >= iterations:
                break
            self.sleeper(interval_seconds)
        return settled

    def _settle(self, approval: dict[str, Any]) -> dict[str, Any] | None:
        run_id = approval["run_id"]
        approval_id = approval["approval_id"]
        client = self.client_factory()
        result = self.orchestrator.wait_infoflow_approval(
            run_id,
            approval_id,
            approval["input_hash"],
            client,
            STRICT_APPROVAL_TIMEOUT_SECONDS,
        )
        reason = result.get("reason_code")
        if reason == "PENDING":
            return self._nudge(approval, client)
        decision = result.get("effective_decision")
        outcome = {
            "run_id": run_id,
            "approval_id": approval_id,
            "action": approval.get("action"),
            "reason_code": reason,
            "effective_decision": decision,
        }
        if reason == "OK" and decision in {"APPROVE", "REJECT"}:
            self._acknowledge(approval, decision, self._responder(approval_id))
        elif reason == "APPROVAL_TIMEOUT":
            self._acknowledge(approval, "TIMEOUT", None)
            reissued = self._reissue(approval, client)
            if reissued is not None:
                outcome["reissued_approval_id"] = reissued
        return outcome

    def _nudge(self, approval: dict[str, Any], client: Any) -> dict[str, Any] | None:
        """Repeat the card as the deadline nears, in case the reply never arrived."""
        remaining = self._remaining(approval.get("deadline_at"))
        if remaining is None or remaining <= 0:
            return None
        bucket = next((index for window, index in _NUDGE_BUCKETS if remaining <= window), 0)
        if bucket == 0:
            return None
        approval_id = approval["approval_id"]
        key = f"{_NUDGE_KEY}:{approval_id}:{bucket}"
        if self.orchestrator.state.idempotency_result(key) is not None:
            return None
        recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
        if not recipients:
            return None
        card = _delivered_content(client, approval)
        try:
            receipt = deliver_markdown(
                self.notify_client,
                self.orchestrator.state,
                approval["run_id"],
                recipients,
                lambda mention: _nudge_markdown(approval, card, remaining, mention and recipients),
            )
        except Exception as error:  # noqa: BLE001 - a failed reminder must not stop the loop
            return {
                "run_id": approval["run_id"],
                "approval_id": approval_id,
                "action": approval.get("action"),
                "reason_code": "APPROVAL_REMINDER_FAILED",
                "detail": str(error),
            }
        self.orchestrator.state.save_idempotency_result(key, {"receipt": receipt})
        return {
            "run_id": approval["run_id"],
            "approval_id": approval_id,
            "action": approval.get("action"),
            "reason_code": "APPROVAL_REMINDED",
            "reminder": bucket,
        }

    def _reissue(self, approval: dict[str, Any], client: Any) -> str | None:
        """A timed-out approval is terminal, so recovery means a fresh bound attempt."""
        if not self.reissue:
            return None
        approval_id = approval["approval_id"]
        key = f"{_REISSUE_KEY}:{approval_id}"
        if self.orchestrator.state.idempotency_result(key) is not None:
            return None
        payload = _delivered_payload(client, approval)
        result = self.orchestrator.reissue_infoflow_approval(
            approval["run_id"],
            str(approval.get("action") or ""),
            approval["input_hash"],
            member_policy=approval.get("member_policy") or {},
            evidence=payload.get("evidence") or {},
            infoflow_client=self.client_factory(),
        )
        self.orchestrator.state.save_idempotency_result(
            key, {"reason_code": result.get("reason_code"), "approval_id": result.get("approval_id")}
        )
        return result.get("approval_id") if result.get("reason_code") in {None, "OK", "PENDING"} else None

    def _remaining(self, deadline_at: Any) -> float | None:
        try:
            deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if deadline.tzinfo is None:
            return None
        return (deadline.astimezone(timezone.utc) - self.clock()).total_seconds()

    def _responder(self, approval_id: str) -> str | None:
        effective = [
            response
            for response in self.orchestrator.approvals.responses(approval_id)
            if response.get("effective")
        ]
        return effective[-1].get("responder") if effective else None

    def _acknowledge(self, approval: dict[str, Any], decision: str, responder: str | None) -> None:
        """Tell the approvers the decision landed; send it once per approval.

        The decision is already in the ledger by the time this runs, so a failed
        message is only a missing courtesy: it must not take the watcher down and
        leave every later reply unread.
        """
        approval_id = approval["approval_id"]
        key = f"{_ACK_KEY}:{approval_id}:{decision}"
        if self.orchestrator.state.idempotency_result(key) is not None:
            return
        recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
        if not recipients:
            return
        try:
            receipt = deliver_markdown(
                self.notify_client,
                self.orchestrator.state,
                approval["run_id"],
                recipients,
                lambda mention: _ack_markdown(approval, decision, responder),
            )
        except Exception:  # noqa: BLE001 - the ledger already holds the decision
            return
        self.orchestrator.state.save_idempotency_result(key, {"receipt": receipt})


def _delivered_payload(client: Any, approval: dict[str, Any]) -> dict[str, Any]:
    """Recover the card and evidence the approvers were sent, when they were stored."""
    receipt = next(
        (
            entry.get("receipt")
            for entry in approval.get("delivery_receipts", [])
            if entry.get("channel") == "infoflow"
        ),
        None,
    )
    request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
    transport = getattr(client, "transport", None)
    if not isinstance(request_id, str) or not hasattr(transport, "delivered"):
        return {}
    payload = transport.delivered(request_id)
    return payload if isinstance(payload, dict) else {}


def _delivered_content(client: Any, approval: dict[str, Any]) -> str:
    return str(_delivered_payload(client, approval).get("content") or "")


def _nudge_markdown(
    approval: dict[str, Any],
    card: str,
    remaining_seconds: float,
    mention: Any = None,
) -> str:
    action = approval.get("action") or "gate"
    hours = max(1, int(remaining_seconds // 3600))
    head = [
        f"## tom-autodev {action} 审批待回复",
        "",
    ]
    if mention:
        head += [f"**待审批** {mention_line(list(mention))}", ""]
    head += [
        f"**剩余** 约 {hours} 小时后超时，超时后该闸门不能补批，只能重新发起",
        "",
    ]
    if card:
        return "\n".join([*head, card])
    approval_id = approval["approval_id"]
    return "\n".join(
        [
            *head,
            "**同意请回复**",
            f"APPROVE {approval_id}",
            "",
            "**驳回请回复**",
            f"REJECT {approval_id}",
        ]
    )


def _later(value: Any, than: Any) -> bool:
    """True when both timestamps parse and the first is not older than the second."""
    try:
        first = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        second = datetime.fromisoformat(str(than).replace("Z", "+00:00"))
    except ValueError:
        return False
    if first.tzinfo is None or second.tzinfo is None:
        return False
    return first >= second


def _resume_markdown(
    brief: dict[str, Any], approval: dict[str, Any], mention: Any = None
) -> str:
    """Name the gate that is done, the step that is next, and the one action needed."""
    gate = approval.get("action") or "gate"
    lines = [f"## tom-autodev {gate} 已通过，等你在 IDE 继续", ""]
    if mention:
        lines += [f"**待操作** {mention_line(list(mention))}", ""]
    lines += [
        f"**当前阶段** {brief['state']}",
        f"**下一步** {brief['next']['text']}",
        "",
        "**请在 Comate 里回复**",
        "继续",
        "",
        "> 该闸门已落账，流程停在这里只是因为阶段推进需要 Comate 执行。",
        f"> run_id `{brief['run_id'][:12]}…`",
    ]
    return "\n".join(lines)


def _ack_markdown(approval: dict[str, Any], decision: str, responder: str | None) -> str:
    action = approval.get("action") or "gate"
    if decision == "TIMEOUT":
        return "\n".join(
            [
                f"## tom-autodev {action} 审批超时",
                "",
                "**结果** 未在截止前收到决策，流程已停在该闸门",
                f"**approval_id** `{approval['approval_id'][:12]}…`",
                "",
                "> 需要重新发起审批；内容若已变更，会绑定新的 input_hash。",
            ]
        )
    landed = "已同意" if decision == "APPROVE" else "已驳回"
    following = (
        "Comate 会在下一步继续该 run。" if decision == "APPROVE" else "Comate 不会继续该闸门之后的动作。"
    )
    return "\n".join(
        [
            f"## tom-autodev {action} {landed}",
            "",
            f"**决策** {decision}",
            f"**决策人** {responder or '-'}",
            f"**approval_id** `{approval['approval_id'][:12]}…`",
            "",
            f"> 决策已写入审批账本，后续回复不会改变结果。{following}",
        ]
    )


def _ide_turn_markdown(brief: dict[str, Any], mention: Any = None) -> str:
    """Say what landed, what is next, and that the next move happens in the IDE."""
    lines = [f"## tom-autodev 阶段已落账，等你在 IDE 继续", ""]
    if mention:
        lines += [f"**待操作** {mention_line(list(mention))}", ""]
    lines += [
        f"**当前阶段** {brief['state']}",
        f"**下一步** {brief['next']['text']}",
    ]
    if brief.get("gate"):
        lines.append(f"**之后要批** {brief['gate']}（卡片会在阶段执行完之后发出）")
    lines += [
        "",
        "**请在 Comate 里回复**",
        "继续",
        "",
        "> 流程停在这里是因为阶段推进需要 Comate 执行，现在没有等你决定的闸门。",
        f"> run_id `{brief['run_id'][:12]}…`",
    ]
    return "\n".join(lines)


def notify_every_parked_run(orchestrator: Any, notify_client: Any) -> dict[str, Any]:
    """Notify for every run that is parked waiting for the IDE.

    A stop is a fact about the session, not about one run, and whoever stopped it may
    not have touched the orchestrator at all. So this takes no run id: it asks the
    ledger which runs are live and lets `notify_ide_turn` decide each one, which keeps
    the per-event suppression and the approval-card suppression in one place.
    """
    notices = []
    for latest in orchestrator.state.latest_states():
        if latest.get("state") in _TERMINAL_STATES:
            continue
        run_id = latest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        try:
            notice = notify_ide_turn(orchestrator, run_id, notify_client)
        except Exception as error:  # noqa: BLE001 - a missed notice must not fail a stop
            notice = {"ok": False, "reason_code": "IDE_TURN_NOTICE_FAILED", "detail": str(error)}
        notices.append({"run_id": run_id, **{
            key: notice.get(key) for key in ("ok", "reason_code") if key in notice
        }})
    return {"ok": True, "reason_code": "OK", "notices": notices}


def notify_ide_turn(orchestrator: Any, run_id: str, notify_client: Any) -> dict[str, Any]:
    """Tell the operator in 如流 that the run is parked waiting for the IDE.

    Suppressed only while an approval is actually out and undecided: that card asks
    the same person for the same move, and `_resume_markdown` covers the hand-back
    after it lands. `brief["gate"]` is *not* that condition — it names the gate the
    next action will eventually open, so for a phase Comate has yet to run no card
    exists and nobody has been told anything. Treating it as coverage is what kept
    every gated stop silent. Keyed on the latest event so a repeated CLI call does
    not resend.
    """
    import run_brief

    brief = run_brief.build(orchestrator, run_id)
    if brief.get("state") in _TERMINAL_STATES or brief.get("state") == "RUN_NOT_FOUND":
        return {"ok": True, "reason_code": "NOTHING_TO_NOTIFY", "run_id": run_id}
    if brief.get("waiting_on"):
        return {"ok": True, "reason_code": "APPROVAL_CARD_COVERS_IT", "run_id": run_id}
    events = orchestrator.state.events(run_id)
    if not events:
        return {"ok": True, "reason_code": "NOTHING_TO_NOTIFY", "run_id": run_id}
    key = f"{_IDE_TURN_KEY}:{run_id}:{events[-1]['event_id']}"
    if orchestrator.state.idempotency_result(key) is not None:
        return {"ok": True, "reason_code": "ALREADY_NOTIFIED", "run_id": run_id}
    recipients = sorted(_ide_turn_recipients(orchestrator, run_id))
    if not recipients:
        return {"ok": True, "reason_code": "NO_RECIPIENTS", "run_id": run_id}
    try:
        receipt = deliver_markdown(
            notify_client,
            orchestrator.state,
            run_id,
            recipients,
            lambda mention: _ide_turn_markdown(brief, recipients if mention else None),
        )
    except Exception as error:  # noqa: BLE001 - a missed notice must not fail the phase
        return {"ok": False, "reason_code": "IDE_TURN_NOTICE_FAILED", "detail": str(error)}
    orchestrator.state.save_idempotency_result(key, {"receipt": receipt})
    return {"ok": True, "reason_code": "OK", "run_id": run_id, "receipt": receipt}


def _ide_turn_recipients(orchestrator: Any, run_id: str) -> set[str]:
    """The infoflow members the project profile already nominates for approvals."""
    pinned = orchestrator._runtime_profile(run_id)
    profile = pinned.get("profile") if isinstance(pinned, dict) else None
    channels = profile.get("approval_channels") if isinstance(profile, dict) else None
    members = channels.get("role_members") if isinstance(channels, dict) else None
    if not isinstance(members, dict):
        return set()
    found: set[str] = set()
    for value in members.values():
        if isinstance(value, list):
            found.update(item for item in value if isinstance(item, str) and item)
    return found
