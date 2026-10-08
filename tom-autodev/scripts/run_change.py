"""Redo a phase without restarting the run: revise after a REJECT, or rewind under approval.

Every approval in a run is bound to the action that produced its draft, and an
action is identified by the event the run is sitting on. So the way to redo work
is not to rewrite anything but to append one more event: the phase is entered
again, the producer gets a new action and job, and the gate is asked again for the
new hash. Earlier events, artifacts and approvals stay as the audit trail, the same
way a workflow reset keeps the original history and continues from a chosen point.

Two entry points, each authorized by a human decision that already exists:

    * ``revise`` — a REVISE of the current producer gate (G1/G2/G3/G4/G5) plus the
  reviewer's feedback. The same state is entered again with that feedback and the
  rejected draft as context, so the next draft is a response to the review rather
  than a fresh guess, and the gate shows what changed.
* ``rewind`` — a CHANGE_REQUEST approval naming the target state, the reason and,
  optionally, a profile edit. Repository facts (``test_repo``/``business_repos``)
  may move only here, and only while no workspace was ever bound to them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import workflow_spec
from execution_guard import guard_execution

ACTION = "CHANGE_REQUEST"
_REQUEST_KEY = "run-change-request"
_REWIND_KEY = "run-change"
_REVISE_KEY = "run-revise"
_DESIGN_ORDER = ("GRILL", "SPEC", "TASKS")
# States that exist only once code was planned or written; a run that has been in one
# cannot be rewound from WORKSPACE, since its Reviews would be reused against a new Spec.
_CODE_STATES = frozenset({"PLAN", "IMPLEMENT", "REVIEW", "SUBMIT", "IPIPE", "DIAGNOSE", "RELEASE"})
# The reply template shows this in place of the feedback; copied verbatim it is no feedback.
FEEDBACK_PLACEHOLDER = "你的修改意见"
FEEDBACK_PLACEHOLDERS = frozenset({FEEDBACK_PLACEHOLDER, "修改意见"})
_DECISION_COMMAND = re.compile(r"^\s*(同意|需修改|驳回|APPROVE|REVISE|REJECT|CHOICE)(?:\s|$)", re.IGNORECASE)


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
            hint="只有 GRILL/SPEC/TASKS/PLAN/IMPLEMENT 可按修改意见原地重做；更早阶段用 rewind，"
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
    if approval.get("effective_decision") not in {"REVISE", "REJECT"}:
        return _failure("APPROVAL_NOT_REJECTED", run_id, approval_id=approval_id,
                        decision=approval.get("effective_decision"))
    action, job_id, draft = _current_draft(orchestrator, run_id)
    if (
        draft is None
        or action.get("source_event_id") != current.get("event_id")
        or _draft_approval_hash(action, draft) != approval.get("input_hash")
    ):
        # Only the REJECT of the draft the run is waiting on can send that phase back.
        return _failure("APPROVAL_STALE", run_id, approval_id=approval_id)
    text = (feedback or "").strip() or _journal_feedback(orchestrator, approval)
    if not text:
        return _failure(
            "FEEDBACK_REQUIRED", run_id, approval_id=approval_id,
            hint="说明需要修改什么；在如流点「需修改」后直接回复意见，或回复 `需修改 <审批ID> 修改意见`",
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


def rewind_available(events: list[dict[str, Any]], state: Any) -> bool:
    """Whether a CHANGE_REQUEST could move this run at all from where it is."""
    if state not in workflow_spec.REWIND_TARGETS:
        return False
    return not (state == "WORKSPACE" and any(event.get("state") in _CODE_STATES for event in events))


def _redo(state: str, target: str) -> list[str]:
    order = list(_DESIGN_ORDER)
    start = order.index(target)
    if state in order:
        return order[start:order.index(state) + 1]
    return order[start:] + [state] if state != "ARCHITECTURE_REVIEW" else order[start:]


def plan(
    orchestrator: Any,
    run_id: str,
    target: str,
    reason: str,
    actor: str,
    previous_profile: str | Path | None = None,
) -> dict[str, Any]:
    """Say whether this rewind is allowed and exactly what the CHANGE_REQUEST would bind."""
    events = orchestrator.state.events(run_id)
    if not events:
        return _failure("RUN_NOT_FOUND", run_id)
    current = events[-1]
    state = current.get("state")
    allowed = workflow_spec.REWIND_TARGETS.get(state)
    if allowed is None:
        return _failure(
            "REWIND_NOT_SUPPORTED", run_id, state=state,
            hint="代码阶段之后的返工经 DIAGNOSE 路由到 SPEC/PLAN；已结束的运行需新建",
        )
    if target not in allowed:
        return _failure("REWIND_TARGET_FORBIDDEN", run_id, state=state, target=target,
                        allowed=sorted(allowed))
    if not rewind_available(events, state):
        return _failure(
            "REWIND_AFTER_CODE_FORBIDDEN", run_id, state=state,
            hint="已有任务进入过计划/实现，回退会让旧 Review 对新 Spec 复用；请 stop 后新建运行",
        )
    if not isinstance(reason, str) or not reason.strip():
        return _failure("REASON_REQUIRED", run_id)
    if not isinstance(actor, str) or not actor.strip():
        return _failure("ACTOR_REQUIRED", run_id)
    # Approval cards are notifications, not run effects: an unconfirmed card,
    # including this change request's own, must not hold the rewind back.
    if any(
        not str(item.get("operation") or "").startswith("approval.")
        for item in orchestrator.state.pending_intents(run_id)
    ):
        return _failure("RECOVERY_REQUIRED", run_id,
                        hint="有已发出但未确认的外部写入，先 resume 收敛再回退")
    profile = None
    intake = events[0].get("payload") or {}
    if previous_profile is not None:
        import profile_repin

        never_bound = not any(event.get("state") == "WORKSPACE" for event in events)
        prepared = profile_repin.plan(
            orchestrator, run_id, previous_profile,
            extra_keys=profile_repin.REWIND_KEYS if never_bound else frozenset(),
        )
        if not prepared.get("ok"):
            if prepared.get("reason_code") == "REPIN_FIELD_FORBIDDEN" and not never_bound:
                prepared["hint"] = "运行已绑定过工作区，仓库字段不能再改；请新建运行"
            return prepared
        confirmation_error = _confirmation_error(intake.get("profile_path"))
        if confirmation_error is not None:
            return _failure(confirmation_error["reason_code"], run_id, **{
                key: value for key, value in confirmation_error.items() if key != "reason_code"
            })
        profile = {
            "old_hash": prepared["old_hash"],
            "new_hash": prepared["new_hash"],
            "changed_keys": prepared["changed_keys"],
            "repin_input_hash": prepared["input_hash"],
        }
    else:
        from phase_protocol import _pinned_profile_error
        import profile_repin

        if _pinned_profile_error(events, profile_repin.record_for(orchestrator.state, run_id)):
            return _failure("PROFILE_CHANGE_UNDECLARED", run_id,
                            hint="profile 已被修改：用 --previous-profile 提供被钉住的旧副本，随回退一并审批")
    binding = {
        "run_id": run_id,
        "action": ACTION,
        "from_state": state,
        "from_event_id": current["event_id"],
        "target": target,
        "reason": reason.strip(),
        "actor": actor.strip(),
        "profile": profile,
        "redo": _redo(state, target),
    }
    return {
        "ok": True,
        "reason_code": "OK",
        **binding,
        "profile_path": intake.get("profile_path"),
        "previous_profile": str(Path(previous_profile).expanduser().resolve()) if previous_profile else None,
        "input_hash": _hash(binding),
    }


def _confirmation_error(profile_path: Any) -> dict[str, Any] | None:
    """An edited profile whose recorded confirmation no longer covers its facts.

    Pinning it would land the run on PROFILE_CONFIRMATION_MISMATCH right after the
    rewind, and fixing the file then needs yet another pin move.
    """
    from profile_wizard import confirmation_hash, confirmed_inputs
    from project_registry import load_profile

    if not isinstance(profile_path, str):
        return None
    loaded = load_profile(Path(profile_path), check_paths=False)
    profile = loaded.get("profile") if loaded.get("ready") else None
    confirmation = profile.get("profile_confirmation") if isinstance(profile, dict) else None
    if not isinstance(confirmation, dict):
        return None
    expected = confirmed_inputs(profile)
    if confirmation.get("confirmed_inputs_hash") == confirmation_hash(expected):
        return None
    return {
        "reason_code": "PROFILE_CONFIRMATION_MISMATCH",
        "confirmed_inputs": expected,
        "hint": "改过的 profile 需重新确认：核对 confirmed_inputs 后更新 profile_confirmation（confirmed_by 与 confirmed_inputs_hash）",
    }


def snapshot(state: Any, run_id: str, input_hash: str) -> dict[str, Any] | None:
    found = state.idempotency_result(f"{_REQUEST_KEY}:{run_id}:{input_hash}")
    return found if isinstance(found, dict) else None


def summary_lines(prepared: dict[str, Any]) -> list[str]:
    lines = [
        f"当前阶段 {prepared['from_state']} → 回到 {prepared['target']}",
        f"将重新产出并重新审批：{' → '.join(prepared['redo'])}",
        f"原因：{prepared['reason']}",
        f"发起人：{prepared['actor']}",
    ]
    profile = prepared.get("profile")
    if isinstance(profile, dict):
        lines.append(f"同时改 profile 字段：{'、'.join(profile['changed_keys'])}"
                     f"（{profile['old_hash'][:12]}… → {profile['new_hash'][:12]}…）")
    if prepared["target"] != "GRILL":
        lines.append(f"{prepared['target']} 之前的已批准产物保持不变；若其中写死了要改的内容，请回到更早阶段")
    lines.append("既有事件、产物和审批记录不删除；回退后的每一版仍需各自的门")
    return lines


def _members(orchestrator: Any, run_id: str, prepared: dict[str, Any]) -> tuple[list[str] | None, dict[str, Any] | None]:
    import yaml

    source = prepared.get("previous_profile")
    if source:
        # The approvers are the people the run was pinned with, not whoever the
        # edited file names.
        try:
            profile = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            return None, _failure("PREVIOUS_PROFILE_INVALID", run_id, detail=str(error))
        channels = profile.get("approval_channels") if isinstance(profile, dict) else None
        role_members = channels.get("role_members") if isinstance(channels, dict) else None
        if not isinstance(role_members, dict):
            return None, _failure("MEMBER_CONFIRMATION_REQUIRED", run_id)
        members = sorted({
            value for values in role_members.values() if isinstance(values, list)
            for value in values if isinstance(value, str) and value
        })
        return (members, None) if members else (None, _failure("MEMBER_CONFIRMATION_REQUIRED", run_id))
    from workflow_spec_migration import _members as pinned_members

    return pinned_members(orchestrator, run_id)


@guard_execution
def request(
    orchestrator: Any,
    run_id: str,
    target: str,
    reason: str,
    actor: str,
    previous_profile: str | Path | None = None,
    *,
    comate_client: Any,
    infoflow_client: Any,
) -> dict[str, Any]:
    """Open the CHANGE_REQUEST gate for one exact rewind."""
    prepared = plan(orchestrator, run_id, target, reason, actor, previous_profile)
    if not prepared.get("ok"):
        return prepared
    members, error = _members(orchestrator, run_id, prepared)
    if error is not None:
        return error
    key = f"{_REQUEST_KEY}:{run_id}:{prepared['input_hash']}"
    if orchestrator.state.idempotency_result(key) is None:
        orchestrator.state.save_idempotency_result(key, prepared)
    references = [
        {"label": "当前阶段事件", "ref": prepared["from_event_id"]},
        {"label": "回退目标", "ref": prepared["target"]},
    ]
    if prepared.get("previous_profile"):
        references += [
            {"label": "已校验的旧 profile", "ref": prepared["previous_profile"]},
            {"label": "待采用的新 profile", "ref": str(prepared.get("profile_path") or "")},
        ]
    opened = orchestrator.request_infoflow_approval(
        run_id,
        ACTION,
        prepared["input_hash"],
        member_policy={"comate": members, "infoflow": members},
        evidence={
            "gate": {
                "subject": f"变更请求：回到 {prepared['target']} 重新产出",
                "effect": f"{prepared['from_state']} → {prepared['target']}，之后各阶段重新生成并重新审批",
                "summary": summary_lines(prepared),
                "references": references,
            },
            "target": prepared["target"],
            "reason": prepared["reason"],
            "profile": prepared["profile"],
        },
        comate_client=comate_client,
        infoflow_client=infoflow_client,
    )
    return {**prepared, "approval": opened}


@guard_execution
def apply(orchestrator: Any, run_id: str, approval_id: str) -> dict[str, Any]:
    """Carry out an approved CHANGE_REQUEST: move the profile pin if asked, then re-enter."""
    replay = orchestrator.state.idempotency_result(f"{_REWIND_KEY}:{approval_id}")
    if isinstance(replay, dict):
        return {**replay, "reason_code": "ALREADY_REWOUND"}
    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict) or approval.get("run_id") != run_id or approval.get("action") != ACTION:
        return _failure("APPROVAL_NOT_FOUND", run_id, approval_id=approval_id)
    if approval.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED", run_id, approval_id=approval_id,
                        decision=approval.get("effective_decision"))
    prepared = snapshot(orchestrator.state, run_id, approval["input_hash"])
    if prepared is None:
        return _failure("CHANGE_REQUEST_MISSING", run_id, approval_id=approval_id)
    events = orchestrator.state.events(run_id)
    if not events or events[-1].get("event_id") != prepared["from_event_id"]:
        return _failure("RUN_MOVED", run_id, approval_id=approval_id,
                        hint="运行已离开发起变更时的位置，这次批准不再适用；请按当前位置重新发起")
    fresh = plan(
        orchestrator, run_id, prepared["target"], prepared["reason"],
        prepared["actor"], prepared.get("previous_profile"),
    )
    if not fresh.get("ok") or fresh.get("input_hash") != approval["input_hash"]:
        return _failure("CHANGE_REQUEST_DRIFTED", run_id, approval_id=approval_id,
                        detail=fresh.get("reason_code"))
    profile = prepared.get("profile")
    extra = []
    if isinstance(profile, dict):
        import profile_repin

        # The pin and the re-entry land in one transaction: a pin moved without its
        # rewind would let the old drafts proceed against the new repositories.
        extra.append(profile_repin.entry(orchestrator.state, run_id, {
            "old_hash": profile["old_hash"],
            "new_hash": profile["new_hash"],
            "changed_keys": profile["changed_keys"],
            "input_hash": profile["repin_input_hash"],
        }, approval_id))
    payload = {
        "reentry": {
            "kind": "REWIND",
            "from_state": prepared["from_state"],
            "from_event_id": prepared["from_event_id"],
            "approval_id": approval_id,
            "reason": prepared["reason"],
            "actor": prepared["actor"],
            "at": _now(),
            "redo": prepared["redo"],
            **({"profile_changed_keys": profile["changed_keys"]} if isinstance(profile, dict) else {}),
        },
    }
    committed = orchestrator.state.commit_transition_result(
        run_id, prepared["from_event_id"], prepared["target"], payload,
        f"{_REWIND_KEY}:{approval_id}",
        {"ok": True, "reason_code": "OK", "from_state": prepared["from_state"],
         "approval_id": approval_id, "redo": prepared["redo"]},
        extra_results=extra,
    )
    return _committed(committed, run_id, approval_id, "ALREADY_REWOUND")


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
    """Whether a revise/rewind landed after this approval was opened.

    The gate it asked belongs to an action the run has left, so it must not be
    reissued, nudged or listed as waiting. Pin moves (policy migration, profile
    re-pin) are about the run rather than a position in it and are never superseded.
    """
    from approval_ledger import gate_of

    if gate_of(approval.get("action") or "") in {"WORKFLOW_SPEC_MIGRATION", "PROFILE_REPIN"}:
        return False
    created = approval.get("created_at") or ""
    return any(
        isinstance((event.get("payload") or {}).get("reentry"), dict)
        and (event.get("created_at") or "") > created
        for event in state.events(run_id)
    )
