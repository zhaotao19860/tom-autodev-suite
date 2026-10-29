from __future__ import annotations

import workflow_spec
from execution_guard import execution_guard, guard_execution, migration_exempt

import time
from datetime import datetime, timezone
from typing import Any

from approval_contract import STRICT_APPROVAL_TIMEOUT_SECONDS
from approval_delivery import _deliver_approval_card, corrected_content, deliver_markdown, mention_line
from approval_ledger import gate_of


_ACK_KEY = "approval.ack"
_NUDGE_KEY = "approval.nudge"
_REISSUE_KEY = "approval.reissue"
_RESUME_KEY = "resume-notice"
_RESUME_HANDOFF_PREFIX = "approval-resume"
_IDE_TURN_KEY = "ide-turn-notice"
# Reminders get more urgent as the deadline approaches; the bucket index keeps each
# reminder idempotent without adding state of its own.
_NUDGE_BUCKETS = ((1800, 3), (7200, 2), (21600, 1))
# A finished run has nothing to report; keep this in step with `transition_policy`.
_TERMINAL_STATES = frozenset({"RELEASE_SUCCESS", "STOPPED"})


def _claim_action(state: Any, run_id: str, operation: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Claim an outbound action, preserving compatibility with test/legacy stores."""
    claim_intent = getattr(state, "claim_intent", None)
    lookup = getattr(state, "result_by_idempotency_key", None)
    if callable(claim_intent):
        claim = claim_intent(run_id, operation, key, payload)
        if claim.get("status") == "CONFLICT":
            return claim
        if claim.get("status") == "EXISTING":
            completed = lookup(key) if callable(lookup) else None
            if completed is not None:
                receipt = completed.get("receipt") if isinstance(completed, dict) else None
                response = receipt.get("response") if isinstance(receipt, dict) else None
                return {"status": "DONE", "response": response}
            return {"status": "QUERY", "intent": claim.get("intent")}
        return claim
    legacy = state.idempotency_result(key)
    if legacy is not None:
        return {"status": "DONE", "response": legacy}
    return {"status": "CLAIMED_LEGACY"}


def _record_action(state: Any, claim: dict[str, Any], key: str, response: dict[str, Any]) -> None:
    if claim.get("status") == "CLAIMED":
        state.receipt(claim["intent"]["intent_id"], response, [])
    else:
        state.save_idempotency_result(key, response)


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
        code_changed: Any | None = None,
    ):
        self.orchestrator = orchestrator
        # Returns True once the control-plane code on disk differs from what this
        # process started with; the loop then stops so the supervisor runs new code.
        self.code_changed = code_changed
        self.stopped_for_code_change = False
        self.client_factory = client_factory
        self.notify_client = notify_client
        self.sleeper = sleeper or time.sleep
        self.reporter = reporter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reissue = reissue

    def tick(self) -> list[dict[str, Any]]:
        outcomes = []
        # Starting the gateway is also an effect. If every known run is blocked,
        # return the policy failures before bringing up any external client.
        run_ids = {item["run_id"] for item in self.orchestrator.state.latest_states()}
        run_ids.update(item["run_id"] for item in self.orchestrator.approvals.pending())
        # A run whose only open gate is its own migration is not unreachable: that
        # gate is exactly what unblocks the drift, so returning policy failures here
        # would leave the single-run case permanently unanswerable.
        migrating = {
            approval["run_id"] for approval in self.orchestrator.approvals.pending()
            if migration_exempt(approval.get("action"))
        }
        blocked = [
            execution_guard(self.orchestrator.state, run_id)
            for run_id in sorted(run_ids) if run_id not in migrating
        ]
        if blocked and all(item is not None for item in blocked) and not migrating:
            return blocked
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
        for revised in self._awaiting_feedback():
            outcomes.append(revised)
            if self.reporter is not None:
                self.reporter(revised)
        for progress in self._progress():
            outcomes.append(progress)
            if self.reporter is not None:
                self.reporter(progress)
        for handoff in self._handoff():
            outcomes.append(handoff)
            if self.reporter is not None:
                self.reporter(handoff)
        if callable(getattr(self.notify_client, "send_work_card", None)):
            from work_card import refresh_all

            for outcome in refresh_all(self.orchestrator, self.notify_client):
                outcomes.append(outcome)
                if self.reporter is not None:
                    self.reporter(outcome)
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
            blocked = execution_guard(self.orchestrator.state, run_id)
            if blocked is not None:
                notices.append(blocked)
                continue
            brief = build(self.orchestrator, run_id)
            if brief["waiting_on"] or brief["in_flight"] or brief.get("blocked"):
                continue
            if brief["next"]["owner"] != "Agent":
                continue
            approval = self._approved_gate(run_id, brief.get("gate"), latest.get("created_at"))
            if approval is None:
                continue
            handoff_id = (
                f"{_RESUME_HANDOFF_PREFIX}-{approval['approval_id']}-"
                f"{latest['event_id']}"
            )
            try:
                handoff = self.orchestrator.state.record_handoff(
                    run_id,
                    handoff_id,
                    {
                        "kind": "APPROVAL_RESUME",
                        "run_id": run_id,
                        "event_id": latest["event_id"],
                        "approval_id": approval["approval_id"],
                        "input_hash": approval["input_hash"],
                        "action": approval.get("action"),
                    },
                )
            except Exception as error:  # noqa: BLE001 - preserve the gate decision
                notices.append({
                    "reason_code": "RESUME_HANDOFF_FAILED",
                    "run_id": run_id,
                    "detail": str(error),
                })
                continue
            key = f"{_RESUME_KEY}:{run_id}:{latest['event_id']}:{approval['approval_id']}"
            if self.orchestrator.state.idempotency_result(key) is not None:
                continue
            recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
            if not recipients:
                continue
            claim = _claim_action(
                self.orchestrator.state,
                run_id,
                "approval.resume_notice",
                key,
                {
                    "run_id": run_id,
                    "event_id": latest["event_id"],
                    "approval_id": approval["approval_id"],
                    "handoff_id": handoff["handoff_id"],
                },
            )
            if claim["status"] == "DONE":
                continue
            if claim["status"] in {"QUERY", "CONFLICT"}:
                notices.append({
                    "reason_code": "RESUME_NOTICE_QUERY_REQUIRED" if claim["status"] == "QUERY" else "RESUME_NOTICE_CONFLICT",
                    "run_id": run_id,
                })
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
            _record_action(self.orchestrator.state, claim, key, {"receipt": receipt})
            notices.append(
                {
                    "reason_code": "RESUME_NOTICE_SENT",
                    "run_id": run_id,
                    "state": latest["state"],
                    # Deliberately not `approval_id`: this outcome reports a position,
                    # not the settlement of a gate, and a reporter must be able to
                    # tell the two apart.
                    "gate_approval_id": approval["approval_id"],
                    "handoff_id": handoff["handoff_id"],
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
            if gate_of(row.get("action")) != gate or row.get("effective_decision") != "APPROVE":
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
            blocked = execution_guard(self.orchestrator.state, run_id)
            if blocked is not None:
                notices.append(blocked)
                continue
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
            claim = _claim_action(
                self.orchestrator.state,
                run_id,
                "approval.progress_notice",
                key,
                {"run_id": run_id, "event_id": latest["event_id"], "state": latest["state"]},
            )
            if claim["status"] == "DONE":
                continue
            if claim["status"] in {"QUERY", "CONFLICT"}:
                notices.append({
                    "reason_code": "PROGRESS_NOTICE_QUERY_REQUIRED" if claim["status"] == "QUERY" else "PROGRESS_NOTICE_CONFLICT",
                    "run_id": run_id,
                })
                continue
            try:
                # Position goes to the owner privately; the group carries the gates,
                # and a per-phase status line for everyone would drown them.
                self.notify_client.send_markdown(recipients, render(brief))
            except Exception as error:  # noqa: BLE001 - any failure here is operational
                notices.append({"reason_code": "PROGRESS_NOTICE_FAILED", "detail": str(error)})
                continue
            _record_action(self.orchestrator.state, claim, key, {"run_id": run_id, "state": latest["state"]})
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
            if self.code_changed is not None and self.code_changed():
                self.stopped_for_code_change = True
                break
            settled += self.tick()
            rounds += 1
            if iterations is not None and rounds >= iterations:
                break
            self.sleeper(interval_seconds)
        return settled

    def _settle(self, approval: dict[str, Any]) -> dict[str, Any] | None:
        run_id = approval["run_id"]
        # The migration gate stays settleable under drift; every other gate needs the
        # migration first, so its own decision must not be applied yet.
        if not migration_exempt(approval.get("action")):
            blocked = execution_guard(self.orchestrator.state, run_id)
            if blocked is not None:
                return blocked
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
        from run_change import superseded

        stale = superseded(self.orchestrator.state, run_id, approval)
        if reason == "PENDING":
            return None if stale else self._nudge(approval, client)
        decision = result.get("effective_decision")
        outcome = {
            "run_id": run_id,
            "approval_id": approval_id,
            "action": approval.get("action"),
            "reason_code": reason,
            "effective_decision": decision,
        }
        if stale and reason == "APPROVAL_TIMEOUT":
            # A card the run already left: its silence needs no notice and no new card.
            return outcome
        if reason == "OK" and decision in {"APPROVE", "REVISE", "REJECT"}:
            responder = self._responder(approval_id)
            revised = self._revise(approval, responder) if decision == "REVISE" and not stale else None
            if decision == "REJECT" and not stale:
                stopped = self.orchestrator.stop(run_id)
                outcome["stopped_state"] = stopped.get("state")
            if revised is not None and revised.get("ok"):
                outcome["revised_event_id"] = revised.get("event_id")
            acknowledgement = self._acknowledge(
                approval, decision, responder, note=_ack_note(approval, decision, stale, revised)
            )
            if acknowledgement is not None and acknowledgement.get("reason_code") not in {
                "ACKNOWLEDGED", "ALREADY_ACKNOWLEDGED",
            }:
                outcome["acknowledgement_reason_code"] = acknowledgement["reason_code"]
        elif reason == "APPROVAL_TIMEOUT":
            acknowledgement = self._acknowledge(approval, "TIMEOUT", None)
            if acknowledgement is not None and acknowledgement.get("reason_code") not in {
                "ACKNOWLEDGED", "ALREADY_ACKNOWLEDGED",
            }:
                outcome["acknowledgement_reason_code"] = acknowledgement["reason_code"]
            reissued = self._reissue(approval, client)
            if reissued is not None:
                if reissued.get("approval_id"):
                    outcome["reissued_approval_id"] = reissued["approval_id"]
                if reissued.get("reason_code") not in {"REISSUED", "ALREADY_REISSUED"}:
                    outcome["reissue_reason_code"] = reissued["reason_code"]
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
        recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
        if not recipients:
            return None
        claim = _claim_action(
            self.orchestrator.state,
            approval["run_id"],
            "approval.nudge",
            key,
            {"approval_id": approval_id, "bucket": bucket, "input_hash": approval["input_hash"]},
        )
        if claim["status"] == "DONE":
            return None
        if claim["status"] == "QUERY":
            return {
                "run_id": approval["run_id"], "approval_id": approval_id,
                "action": approval.get("action"), "reason_code": "APPROVAL_REMINDER_QUERY_REQUIRED",
            }
        if claim["status"] == "CONFLICT":
            return {
                "run_id": approval["run_id"], "approval_id": approval_id,
                "action": approval.get("action"), "reason_code": "APPROVAL_REMINDER_CONFLICT",
            }
        delivered = _delivered_payload(client, approval)
        # A reminder must repeat what the approvers were last shown, so a delivered
        # correction wins over the original message it corrected.
        card = corrected_content(self.orchestrator.state, approval) or str(delivered.get("content") or "")
        progress = _progress_for(self.orchestrator, approval["run_id"])
        try:
            receipt = deliver_markdown(
                self.notify_client,
                self.orchestrator.state,
                approval["run_id"],
                recipients,
                lambda mention: _nudge_markdown(
                    approval, card, remaining, mention and recipients, progress=progress
                ),
            )
        except Exception as error:  # noqa: BLE001 - a failed reminder must not stop the loop
            return {
                "run_id": approval["run_id"],
                "approval_id": approval_id,
                "action": approval.get("action"),
                "reason_code": "APPROVAL_REMINDER_FAILED",
                "detail": str(error),
            }
        _record_action(self.orchestrator.state, claim, key, {"receipt": receipt})
        # The reminder repeats "同意请回复 …"; without its own card the only buttons
        # are on the original bubble far up the chat.
        try:
            buttons = _deliver_approval_card(
                self.notify_client,
                self.orchestrator.state,
                {**approval, "evidence": delivered.get("evidence") or {}},
                presentation=f"nudge-{bucket}",
            )
        except Exception as error:  # noqa: BLE001 - typed replies still work
            buttons = {"status": "UNKNOWN", "reason_code": type(error).__name__}
        return {
            "run_id": approval["run_id"],
            "approval_id": approval_id,
            "action": approval.get("action"),
            "reason_code": "APPROVAL_REMINDED",
            "reminder": bucket,
            "buttons": buttons.get("status"),
        }

    def _reissue(self, approval: dict[str, Any], client: Any) -> dict[str, Any] | None:
        """A timed-out approval is terminal, so recovery means a fresh bound attempt."""
        if not self.reissue:
            return None
        approval_id = approval["approval_id"]
        key = f"{_REISSUE_KEY}:{approval_id}"
        claim = _claim_action(
            self.orchestrator.state,
            approval["run_id"],
            "approval.reissue",
            key,
            {"approval_id": approval_id, "action": approval.get("action"), "input_hash": approval["input_hash"]},
        )
        if claim["status"] == "DONE":
            response = claim.get("response") or {}
            return {"reason_code": "ALREADY_REISSUED", "approval_id": response.get("approval_id")}
        if claim["status"] == "QUERY":
            return {"reason_code": "APPROVAL_REISSUE_QUERY_REQUIRED"}
        if claim["status"] == "CONFLICT":
            return {"reason_code": "APPROVAL_REISSUE_CONFLICT"}
        payload = _delivered_payload(client, approval)
        try:
            result = self.orchestrator.reissue_infoflow_approval(
                approval["run_id"],
                gate_of(approval.get("action") or ""),
                approval["input_hash"],
                member_policy=approval.get("member_policy") or {},
                evidence=payload.get("evidence") or {},
                infoflow_client=self.client_factory(),
                allow_workflow_spec_migration=migration_exempt(approval.get("action")),
            )
        except Exception as error:  # noqa: BLE001 - the external outcome is unknown
            return {"reason_code": "APPROVAL_REISSUE_QUERY_REQUIRED", "detail": str(error)}
        if not isinstance(result, dict):
            return {"reason_code": "APPROVAL_REISSUE_QUERY_REQUIRED"}
        _record_action(self.orchestrator.state, claim, key, result)
        if result.get("reason_code") in {None, "OK", "PENDING"}:
            return {"reason_code": "REISSUED", "approval_id": result.get("approval_id")}
        return {"reason_code": result.get("reason_code")}

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

    def _revise(self, approval: dict[str, Any], responder: str | None) -> dict[str, Any] | None:
        """Record the redo once the member who chose 需修改 has said what to change.

        Only that member's feedback counts (run_change reads it from the reply journal):
        the text after ``需修改 <id>``, or their next plain message in the chat after a
        button tap. Without it the result is FEEDBACK_REQUIRED and the group is asked.
        """
        if gate_of(approval.get("action") or "") not in workflow_spec.REVISABLE_GATES:
            return self.orchestrator.route_failure(
                approval["run_id"],
                "REVIEW_FAILED",
                {
                    "classification": "CONFIRMED",
                    "approval_id": approval["approval_id"],
                    "input_hash": approval.get("input_hash"),
                    "reason": "审批人选择需修改，后置阶段进入 DIAGNOSE 走修复并重新提交。",
                    "responder": responder,
                },
            )
        try:
            from run_change import revise

            result = revise(
                self.orchestrator, approval["run_id"], approval["approval_id"],
                feedback=None, actor=responder or "infoflow",
            )
        except Exception:  # noqa: BLE001 - the REJECT already landed; redo stays available in the IDE
            return None
        if result.get("ok") or result.get("reason_code") == "FEEDBACK_REQUIRED":
            return result
        return None

    def _awaiting_feedback(self) -> list[dict[str, Any]]:
        """Finish a 需修改 whose feedback arrived after the decision was settled."""
        from run_change import superseded

        outcomes = []
        for latest in self.orchestrator.state.latest_states():
            run_id = latest["run_id"]
            state = latest.get("state")
            if state not in workflow_spec.REVISABLE_STATES:
                continue
            if execution_guard(self.orchestrator.state, run_id) is not None:
                continue
            gate = workflow_spec.STATES[state].output_gate
            for approval in self.orchestrator.approvals.for_run(run_id):
                if (
                    approval.get("effective_decision") != "REVISE"
                    or gate_of(approval.get("action") or "") != gate
                    or self.orchestrator.state.idempotency_result(
                        f"run-revise:{approval['approval_id']}") is not None
                    or superseded(self.orchestrator.state, run_id, approval)
                ):
                    continue
                responder = self._responder(approval["approval_id"])
                revised = self._revise(approval, responder)
                if not isinstance(revised, dict) or not revised.get("ok"):
                    continue
                outcome = {
                    "run_id": run_id, "approval_id": approval["approval_id"],
                    "action": approval.get("action"), "reason_code": "REVISE_FEEDBACK_RECEIVED",
                    "revised_event_id": revised.get("event_id"),
                }
                self._acknowledge(approval, "REVISE_FEEDBACK", responder,
                                  note=_ack_note(approval, "REVISE", False, revised))
                outcomes.append(outcome)
        return outcomes

    def _acknowledge(self, approval: dict[str, Any], decision: str, responder: str | None, *, note: str | None = None) -> dict[str, Any] | None:
        """Tell the approvers the decision landed; send it once per approval.

        The decision is already in the ledger by the time this runs, so a failed
        message is only a missing courtesy: it must not take the watcher down and
        leave every later reply unread.
        """
        approval_id = approval["approval_id"]
        key = f"{_ACK_KEY}:{approval_id}:{decision}"
        recipients = sorted(approval.get("member_policy", {}).get("infoflow", []))
        if not recipients:
            return None
        claim = _claim_action(
            self.orchestrator.state,
            approval["run_id"],
            "approval.ack",
            key,
            {
                "approval_id": approval_id, "decision": decision,
                "responder": responder, "recipients": recipients,
            },
        )
        if claim["status"] == "DONE":
            return {"reason_code": "ALREADY_ACKNOWLEDGED"}
        if claim["status"] == "QUERY":
            return {"reason_code": "APPROVAL_ACK_QUERY_REQUIRED"}
        if claim["status"] == "CONFLICT":
            return {"reason_code": "APPROVAL_ACK_CONFLICT"}
        try:
            progress = _progress_for(self.orchestrator, approval["run_id"])
            receipt = deliver_markdown(
                self.notify_client,
                self.orchestrator.state,
                approval["run_id"],
                recipients,
                lambda mention: _ack_markdown(
                    approval, decision, responder, progress=progress, note=note
                ),
            )
        except Exception as error:  # noqa: BLE001 - the ledger already holds the decision
            return {"reason_code": "APPROVAL_ACK_FAILED", "detail": str(error)}
        _record_action(self.orchestrator.state, claim, key, {"receipt": receipt})
        return {"reason_code": "ACKNOWLEDGED"}


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
    progress: dict[str, Any] | None = None,
) -> str:
    action = approval.get("action") or "gate"
    hours = max(1, int(remaining_seconds // 3600))
    head = [
        f"## tom-autodev {action} 审批待回复",
        "",
    ]
    if mention:
        head += [f"**待审批** {mention_line(list(mention))}", ""]
    if progress:
        from progress_snapshot import render as render_progress

        head += ["**整体进度**", render_progress(progress), ""]
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
            f"同意 {approval_id}",
            "",
            *(
                [
                    "**需修改请回复**",
                    f"需修改 {approval_id} <你的中文修改意见>",
                    "",
                ]
                if gate_of(approval.get("action") or "") in workflow_spec.REVISABLE_GATES
                else []
            ),
            "**停止 run 请回复**",
            f"停止 {approval_id}",
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
        "**驱动本 run 的 agent 会话会自动续跑**",
        "",
        "> 该闸门已落账；若当前会话仍活跃，Stop Hook 会自动交回驱动本 run 的 agent 会话。",
        "> 若会话已结束，请重新打开驱动本 run 的 agent（Comate / Claude Code / Codex）后执行 `resume`。",
        f"> run_id `{brief['run_id'][:12]}…`",
    ]
    progress = brief.get("progress")
    if progress:
        from progress_snapshot import render as render_progress
        lines[4:4] = ["**整体进度**", render_progress(progress), ""]
    return "\n".join(lines)


def reap_stale_resume_handoffs(
    orchestrator: Any, run_id: str, handoffs: list[dict[str, Any]], *, actor: str
) -> list[dict[str, Any]]:
    """Complete provably expired APPROVAL_RESUME handoffs; return the ones that remain.

    A handoff is expired only when ALL hold: it names an event other than the run's
    latest one, its approval is an APPROVE bound to the handoff's run and input_hash,
    and that approval was already consumed (its id is recorded in a later event) or its
    gate no longer matches the current state's gate. Handoffs of the latest event,
    REJECT/TIMEOUT/REVISE approvals and malformed rows are always kept.
    """
    import json

    events = orchestrator.state.events(run_id)
    if not events:
        return list(handoffs)
    latest_id = events[-1].get("event_id")
    spec = workflow_spec.STATES.get(events[-1].get("state"))
    current_gate = spec.output_gate if spec is not None else None
    ids = [event.get("event_id") for event in events]
    kept: list[dict[str, Any]] = []
    for handoff in handoffs:
        data = handoff.get("payload") or {}
        event_id = data.get("event_id")
        approval = orchestrator.approvals.get(data.get("approval_id"))
        expired = (
            isinstance(event_id, str) and event_id and event_id != latest_id
            and isinstance(approval, dict)
            and approval.get("run_id") == run_id
            and approval.get("effective_decision") == "APPROVE"
            and approval.get("input_hash") == data.get("input_hash")
        )
        if expired:
            later = events[ids.index(event_id) + 1:] if event_id in ids else []
            consumed = any(
                str(approval["approval_id"]) in json.dumps(event.get("payload"), sort_keys=True, default=str)
                for event in later
            )
            expired = consumed or gate_of(approval.get("action")) != current_gate
        if not expired:
            kept.append(handoff)
            continue
        orchestrator.state.complete_handoff_audited(handoff, actor=actor, reason="STALE_EVENT")
    return kept


def auto_resume_from_hook(orchestrator: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Hand one approved resume back to the live agent turn that drives the run.

    The hook only injects context and never executes a phase or external write.
    Completing the durable handoff makes repeated Stop callbacks idempotent.
    """
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return {"ok": True, "reason_code": "HOOK_EVENT_IGNORED"}
    if payload.get("status") == "cancelled":
        return {"ok": True, "reason_code": "SESSION_CANCELLED"}
    candidates = []
    for latest in orchestrator.state.latest_states():
        run_id = latest.get("run_id")
        if not isinstance(run_id, str):
            continue
        open_handoffs = [
            handoff for handoff in orchestrator.state.incomplete_handoffs(run_id)
            if (handoff.get("payload") or {}).get("kind") == "APPROVAL_RESUME"
        ]
        if len(open_handoffs) > 1:
            open_handoffs = reap_stale_resume_handoffs(
                orchestrator, run_id, open_handoffs, actor="approval_watch.stop_hook"
            )
        for handoff in open_handoffs:
            candidates.append((str(handoff["handoff_id"]), run_id, handoff.get("payload") or {}))
    if not candidates:
        return {"ok": True, "reason_code": "NO_RESUME_HANDOFF"}
    if len(candidates) > 1:
        return {"ok": True, "reason_code": "MULTIPLE_RESUME_HANDOFFS"}
    handoff_id, run_id, data = sorted(candidates, key=lambda item: item[0])[0]
    # Only the session that drives this run is resumed; any other session (another
    # agent window, an unrelated task) would get a run it has no context for.
    import agent_host
    stopping = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    driver = agent_host.driver_session(orchestrator.state, run_id)
    if not stopping or driver != stopping:
        return {"ok": True, "reason_code": "NOT_DRIVER_SESSION", "run_id": run_id,
                "driver_session": driver, "stopping_session": stopping}
    blocked = execution_guard(orchestrator.state, run_id)
    if blocked is not None:
        return blocked
    events_reader = getattr(orchestrator.state, "events", None)
    latest_events = events_reader(run_id) if callable(events_reader) else []
    if latest_events and latest_events[-1].get("event_id") != data.get("event_id"):
        return {"ok": True, "reason_code": "RESUME_HANDOFF_STALE", "run_id": run_id}
    approval = orchestrator.approvals.get(data.get("approval_id"))
    if (
        not isinstance(approval, dict)
        or approval.get("run_id") != run_id
        or approval.get("effective_decision") != "APPROVE"
        or approval.get("input_hash") != data.get("input_hash")
    ):
        return {"ok": True, "reason_code": "RESUME_HANDOFF_STALE", "run_id": run_id}
    orchestrator.state.complete_handoff(handoff_id)
    return {
        "ok": True,
        "reason_code": "AUTO_RESUME",
        "run_id": run_id,
        "approval_id": approval["approval_id"],
        "input_hash": approval["input_hash"],
        "decision": "block",
        "continue": True,
        "reason": "审批已通过，继续执行 tom-autodev 当前 run",
        "additionalContext": (
            f"审批 {approval['approval_id']} 已通过且与 input_hash 绑定。"
            f"请继续 run_id {run_id}，先读取 next/status 并遵守当前阶段审批，"
            "不要重复提交、重跑或绕过其他闸门。"
        ),
    }


def _ack_note(
    approval: dict[str, Any], decision: str, stale: bool, revised: dict[str, Any] | None
) -> str | None:
    """What actually happens next, so the acknowledgement never promises a step nobody takes."""
    gate = gate_of(approval.get("action") or "")
    if stale:
        return "该卡已被之后的重做/回退取代，本决定不会被使用。"
    if decision == "REVISE" and revised is not None and revised.get("reason_code") == "FEEDBACK_REQUIRED":
        return (
            "请直接在本群回复修改意见（不用带审批 ID，可分多条发）；"
            "收到后自动按意见登记重做，并在 IDE 继续后重新发起本门。"
        )
    if decision == "REVISE" and revised is not None:
        return (
            f"已按意见登记重做（{' '.join(str(revised.get('feedback') or '').split())[:200]}）；"
            "在 IDE 继续后，Agent 生成新版草案并重新发起本门。"
        )
    if decision == "REVISE":
        return "已选择需修改：保留 run 并重新进入当前阶段，Agent 将按审批重点生成新版材料并重新发起本门。"
    if decision == "REJECT":
        return "已停止 run，不会继续后续阶段。"
    if decision == "APPROVE" and gate == "CHANGE_REQUEST":
        return f"变更请求只授权回退本身：在 agent 会话里执行 `rewind --approval-id {approval['approval_id']}`。"
    return None


def _ack_markdown(
    approval: dict[str, Any], decision: str, responder: str | None,
    *, progress: dict[str, Any] | None = None, note: str | None = None,
) -> str:
    action = approval.get("action") or "gate"
    if decision == "TIMEOUT":
        lines = [
            f"## tom-autodev {action} 审批超时",
            "",
            "**结果** 未在截止前收到决策，流程已停在该闸门",
            f"**approval_id** `{approval['approval_id'][:12]}…`",
        ]
        if progress:
            from progress_snapshot import render as render_progress

            lines += ["", "**整体进度**", render_progress(progress)]
        lines += ["", "> 需要重新发起审批；内容若已变更，会绑定新的 input_hash。"]
        return "\n".join(lines)
    landed = {
        "APPROVE": "已同意", "REVISE": "已标记需修改", "REJECT": "已停止 run",
        "REVISE_FEEDBACK": "已收到修改意见",
    }.get(decision, decision)
    following = {
        "APPROVE": "审批已生效；另一渠道无需重复点击。若 IDE 会话仍在运行，会在下一轮自动接续；若会话已结束，请在驱动本 run 的 agent 会话输入“继续”。",
        "REVISE": "run 保留，Agent 按意见重做当前阶段并重新发起审批。",
        "REJECT": "run 已停止，不会继续后续阶段。",
    }.get(decision, "请以审批账本和 run 状态为准。")
    if note:
        replaces = decision in {"APPROVE", "REVISE_FEEDBACK"} or "取代" in note or "回复修改意见" in note
        following = note if replaces else following + note
    lines = [
            f"## tom-autodev {action} {landed}",
            "",
            f"**决策** {'REVISE' if decision == 'REVISE_FEEDBACK' else decision}",
            f"**决策人** {responder or '-'}",
            f"**approval_id** `{approval['approval_id'][:12]}…`",
            "",
            f"> 决策已写入审批账本，后续回复不会改变结果。{following}",
    ]
    if progress:
        from progress_snapshot import render as render_progress

        lines[5:5] = ["", "**整体进度**", render_progress(progress)]
    return "\n".join(lines)


def _progress_for(orchestrator: Any, run_id: str) -> dict[str, Any] | None:
    try:
        from progress_snapshot import build as build_progress

        snapshot = build_progress(orchestrator, run_id)
        return snapshot if isinstance(snapshot, dict) else None
    except Exception:  # noqa: BLE001 - notification detail must not stop the watcher
        return None


def _ide_turn_markdown(brief: dict[str, Any], mention: Any = None) -> str:
    """A parked session is not evidence that its current phase completed."""
    lines = ["## tom-autodev 进度通知（非审批），请在 IDE 继续", "",
             "> 本消息不请求批准，也不会授权任何动作；停驻不代表当前阶段已完成。"]
    if mention:
        lines += [f"**待操作** {mention_line(list(mention))}", ""]
    lines += [
        f"**当前阶段** {brief['state']}",
        f"**下一步** {brief['next']['text']}",
    ]
    progress = brief.get("progress")
    if progress:
        from progress_snapshot import render as render_progress
        lines += ["", "**整体进度**", render_progress(progress)]
    if brief.get("gate"):
        lines.append(f"**阶段闸门** {brief['gate']}（是否已批准以账本为准；本消息不是审批卡）")
    lines += [
        "",
        "**请在驱动本 run 的 agent 会话里回复**",
        "继续",
        "",
        "> 流程停在这里是因为阶段推进需要 agent 执行，现在没有等你决定的闸门。",
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


@guard_execution
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
    claim = _claim_action(
        orchestrator.state,
        run_id,
        "approval.ide_turn_notice",
        key,
        {"run_id": run_id, "event_id": events[-1]["event_id"], "recipients": recipients},
    )
    if claim["status"] == "DONE":
        return {"ok": True, "reason_code": "ALREADY_NOTIFIED", "run_id": run_id}
    if claim["status"] == "QUERY":
        return {"ok": False, "reason_code": "IDE_TURN_NOTICE_QUERY_REQUIRED", "run_id": run_id}
    if claim["status"] == "CONFLICT":
        return {"ok": False, "reason_code": "IDE_TURN_NOTICE_CONFLICT", "run_id": run_id}
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
    _record_action(orchestrator.state, claim, key, {"receipt": receipt})
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
