"""Redo a phase without restarting the run: revise after a REVISE.

Every approval in a run is bound to the action that produced its draft, and an
action is identified by the event the run is sitting on. So the way to redo work
is not to rewrite anything but to append one more event: the phase is entered
again, the producer gets a new action and job, and the gate is asked again for the
new hash. Earlier events, artifacts and approvals stay as the audit trail.

``revise`` takes a REVISE of the current producer gate (G1–G6) plus the reviewer's
feedback. The same state is entered again with that feedback and the rejected draft
as context, so the next draft is a response to the review rather than a fresh guess,
and the gate shows what changed. Going back to an earlier phase is `restart-from`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

import workflow_spec
from execution_guard import guard_execution

_REVISE_KEY = "run-revise"
# The reply template shows this in place of the feedback; copied verbatim it is no feedback.
FEEDBACK_PLACEHOLDER = "你的修改意见"
FEEDBACK_PLACEHOLDERS = frozenset({FEEDBACK_PLACEHOLDER, "修改意见"})
_DECISION_COMMAND = re.compile(r"^\s*(同意|需修改|停止run|停止|驳回|APPROVE|REVISE|REJECT|CHOICE)(?:\s|$)", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _failure(reason_code: str, run_id: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "run_id": run_id, **details}


def _current_draft(orchestrator: Any, run_id: str) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """The read-only current action, its producer job id and the saved draft."""
    from run_brief import _read_only_next

    action = _read_only_next(orchestrator, run_id)
    if not action.get("ok") or not action.get("action_id"):
        return action, "", None
    job_id = f"producer:{action['action_id']}"
    job = orchestrator.state.producer_job(job_id)
    draft = job.get("draft") if isinstance(job, dict) and job.get("status") == "FULFILLED" else None
    return action, job_id, draft if isinstance(draft, dict) else None


def _draft_approval_hash(action: dict[str, Any], draft: dict[str, Any]) -> str:
    from worker_driver import build_envelope

    merged = action.get("phase") == "SPEC" and isinstance(draft.get("spec"), dict)
    return build_envelope(action, draft["spec"] if merged else draft)["approval_input_hash"]


def _journal_feedback(orchestrator: Any, approval: dict[str, Any]) -> str:
    """The text a member typed after ``需修改 <id>`` in 如流, if the revision came that way.

    Only the member who cast the effective REJECT is read, so another person's
    message cannot become the instruction the producer follows.
    """
    from clients.infoflow_reply_client import InfoflowReplyJournal

    responders = {
        str(row.get("responder") or "").split("@", 1)[0]
        for row in orchestrator.approvals.responses(approval["approval_id"])
        if row.get("effective") and row.get("decision") in {"REVISE", "REJECT"}
    }
    responders.discard("")
    if not responders:
        return ""
    lead = re.compile(
        r"^\s*(?:(?:驳回|需修改)\s*)?(?:REJECT|REVISE)\s*"
        + re.escape(approval["approval_id"])
        + r"|^\s*(?:驳回|需修改)\s*"
        + re.escape(approval["approval_id"]),
        re.IGNORECASE,
    )
    found = []
    # A button tap (or a bare reply) carries no text, so the same member's next plain
    # messages in that chat are the feedback, up to their next decision command.
    awaiting: dict[str, Any] = {}
    for message in InfoflowReplyJournal().messages():
        text = message.get("text") or ""
        sender = message.get("sender")
        if sender not in responders:
            continue
        matched = lead.match(text)
        if matched is not None:
            comment = re.sub(r"\s+", " ", text[matched.end():]).strip(" ：:，,。")
            if comment and comment.strip("<>《》[]【】 ") not in FEEDBACK_PLACEHOLDERS:
                found.append(comment)
                awaiting.pop(sender, None)
            else:
                awaiting[sender] = message.get("group_id")
            continue
        if sender not in awaiting:
            continue
        command = _DECISION_COMMAND.match(text)
        if command is not None:
            # Answering another gate in between is not the end of this feedback; asking
            # for changes on another gate, or answering this one again, is.
            verb = command.group(1).upper()
            subject = text[command.end():].split(maxsplit=1)[:1]
            if verb in {"需修改", "REVISE"} or subject == [approval["approval_id"]]:
                awaiting.pop(sender)
            continue
        if message.get("group_id") != awaiting[sender]:
            continue
        comment = re.sub(r"\s+", " ", text).strip(" ：:，,。")
        if comment and comment.strip("<>《》[]【】 ") not in FEEDBACK_PLACEHOLDERS:
            found.append(comment)
    return "；".join(dict.fromkeys(found))


@guard_execution
def revise(
    orchestrator: Any,
    run_id: str,
    approval_id: str,
    *,
    feedback: str | None,
    actor: str,
) -> dict[str, Any]:
    """Re-enter the current design-front state against a REJECT and its feedback."""
    replay = orchestrator.state.idempotency_result(f"{_REVISE_KEY}:{approval_id}")
    if isinstance(replay, dict):
        return {**replay, "reason_code": "ALREADY_REVISED"}
    events = orchestrator.state.events(run_id)
    if not events:
        return _failure("RUN_NOT_FOUND", run_id)
    current = events[-1]
    state = current.get("state")
    if state not in workflow_spec.REVISABLE_STATES:
        return _failure(
            "REVISE_NOT_SUPPORTED", run_id, state=state,
            hint="只有 GRILL/SPEC/TASKS/PLAN/IMPLEMENT/DIAGNOSE 可按修改意见原地重做；更早阶段用 restart-from 新建运行，"
                 "需求变更由 IMPLEMENT/PLAN 上报 USER_REQUIREMENT_CHANGED 经 DIAGNOSE 回到 GRILL",
        )
    if not isinstance(actor, str) or not actor.strip():
        return _failure("ACTOR_REQUIRED", run_id)
    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict) or approval.get("run_id") != run_id:
        return _failure("APPROVAL_NOT_FOUND", run_id, approval_id=approval_id)
    from approval_ledger import gate_of

    gate = workflow_spec.STATES[state].output_gate
    if gate_of(approval.get("action") or "") != gate:
        return _failure("APPROVAL_GATE_MISMATCH", run_id, approval_id=approval_id, expected_gate=gate)
    action, job_id, draft = _current_draft(orchestrator, run_id)
    if (
        draft is None
        or action.get("source_event_id") != current.get("event_id")
        or _draft_approval_hash(action, draft) != approval.get("input_hash")
    ):
        # Only the REJECT of the draft the run is waiting on can send that phase back.
        return _failure("APPROVAL_STALE", run_id, approval_id=approval_id)
    decision = approval.get("effective_decision")
    text = (feedback or "").strip() or _journal_feedback(orchestrator, approval)
    if decision not in {"REVISE", "REJECT"}:
        # Comate path: giving feedback here IS the 需修改 decision. Settle the still
        # PENDING gate as a REVISE on the comate channel (the ledger checks the actor
        # against the member policy and the input hash) so the user does not have to
        # tap the 如流 card first. A gate already APPROVED/TIMED-OUT cannot be revised.
        if decision is not None or not text:
            return _failure("APPROVAL_NOT_REJECTED", run_id, approval_id=approval_id,
                            decision=decision,
                            hint="先在如流点「需修改」并回复意见，或在 Comate 用 `--feedback` 直接提意见")
        settled = orchestrator.approvals.receive(
            approval_id, "REVISE", approval.get("input_hash"), "comate", actor.strip(),
            run_id=run_id, state_store=orchestrator.state,
        )
        if settled.get("reason_code") not in {None, "OK"}:
            return _failure(settled.get("reason_code", "APPROVAL_NOT_REJECTED"),
                            run_id, approval_id=approval_id)
        approval = orchestrator.approvals.get(approval_id)
    if not text:
        return _failure(
            "FEEDBACK_REQUIRED", run_id, approval_id=approval_id,
            hint="说明需要修改什么；在如流点「需修改」后直接回复意见，回复 `需修改 <审批ID> 修改意见`，"
                 "或在 Comate 用 `/tom-autodev <卡片> 需修改 <意见>`",
        )
    # Only the reason travels: a design-front action reads nothing else from its
    # event, and copying the entry payload (previous_state/artifact_id) would let
    # crash recovery mistake the redo for the original SPEC->TASKS hand-off.
    payload: dict[str, Any] = {}
    # PLAN is the only revisable phase whose predecessor (TASKS) has no revisions.
    # Use the already validated action as the source of truth.  Do not copy arbitrary
    # fields from the event: a REVISE event must not become an alternate version pin.
    if state in {"PLAN", "IMPLEMENT"}:
        source_revisions = action.get("source_revisions")
        if isinstance(source_revisions, dict) and source_revisions:
            payload["source_revisions"] = dict(source_revisions)
    if state == "DIAGNOSE":
        # The diagnosis predecessor (failure, producer block) is bound by the entry
        # event, so the redo carries that binding unchanged.
        payload.update({key: value for key, value in (current.get("payload") or {}).items()
                        if key != "reentry"})
    if state == "IMPLEMENT":
        # Keep the redo on the approved plan the rejected Change Set was built from.
        plan = next((item for item in action.get("input_artifacts") or []
                     if isinstance(item, dict) and item.get("phase") == "PLAN"), None)
        if plan is None:
            return _failure("PLAN_PIN_REQUIRED", run_id, approval_id=approval_id)
        payload["plan_artifact_id"] = plan["artifact_id"]
        payload["plan_content_hash"] = plan["content_hash"]
        payload["task_id"] = action.get("task_id")
    payload["reentry"] = {
        "kind": "REVISE",
        "from_state": state,
        "approval_id": approval_id,
        "rejected_input_hash": approval.get("input_hash"),
        "feedback": text,
        "actor": actor.strip(),
        "at": _now(),
        "previous_job_id": job_id,
    }
    committed = orchestrator.state.commit_transition_result(
        run_id, current["event_id"], state, payload, f"{_REVISE_KEY}:{approval_id}",
        {"ok": True, "reason_code": "OK", "approval_id": approval_id,
         "feedback": text, "previous_job_id": job_id},
    )
    return _committed(committed, run_id, approval_id, "ALREADY_REVISED")


def _committed(committed: dict[str, Any], run_id: str, approval_id: str, replay_code: str) -> dict[str, Any]:
    status = committed.get("status")
    if status == "COMMITTED":
        return committed["result"]
    if status == "REPLAY":
        return {**committed["result"], "reason_code": replay_code}
    if status == "SOURCE_EVENT_MISMATCH":
        return _failure("RUN_MOVED", run_id, approval_id=approval_id,
                        hint="运行已离开发起时的位置，这次决定不再适用；请按当前位置重新发起")
    return _failure("RUN_CHANGE_CONFLICT", run_id, approval_id=approval_id, detail=status)


def superseded(state: Any, run_id: str, approval: dict[str, Any]) -> bool:
    """Whether a revise landed after this approval was opened.

    The gate it asked belongs to an action the run has left, so it must not be
    reissued, nudged or listed as waiting.
    """
    created = approval.get("created_at") or ""
    return any(
        isinstance((event.get("payload") or {}).get("reentry"), dict)
        and (event.get("created_at") or "") > created
        for event in state.events(run_id)
    )
