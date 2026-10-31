"""Answer three questions about a run without reading a JSON event dump.

Where is it, is anything in flight, and what does the next step need from whom.
The same lines are printed in the IDE and pushed to 如流, so an operator never has
to reconstruct state from `status` output or guess whether "继续" is even the right
thing to say.
"""
from __future__ import annotations

import inspect
from typing import Any

import workflow_spec
from approval_ledger import gate_of

_TERMINAL_SUCCESS = {"RELEASE_SUCCESS", "STOPPED"}


def _status_label(state: str, action: dict[str, Any], open_gates: list[dict[str, Any]], pending: list[dict[str, Any]], blocked: str | None, approved: dict[str, Any] | None = None, producer_job: dict[str, Any] | None = None, rejected: dict[str, Any] | None = None) -> str:
    if state == "RELEASE_SUCCESS":
        return "已完成"
    if state == "STOPPED":
        return "已停止"
    if pending or blocked == "RECOVERY_REQUIRED":
        return "待恢复"
    if approved and approved.get("action") == "WORKFLOW_SPEC_MIGRATION":
        return "迁移已批准待应用"
    if approved and gate_of(approved.get("action") or "") == "CHANGE_REQUEST":
        return "变更已批准待执行"
    if open_gates:
        return "待审批"
    if approved:
        return "已批准待提交"
    if rejected:
        return "需修改待重做"
    if state == "ARCHITECTURE_REVIEW":
        return "待决定回退"
    if producer_job and producer_job.get("awaiting_answers"):
        return "待答题"
    if producer_job and producer_job.get("status") == "FULFILLED":
        return "草案已保存待审批" if action.get("required_human_gate") else "草案已保存待处理"
    if action.get("child_skill") is not None:
        return "待生成"
    if pending or blocked == "RECOVERY_REQUIRED":
        return "待恢复"
    if blocked:
        return "已阻塞"
    if state == "IPIPE":
        return "等待 iPipe"
    return "待处理"

# What each phase asks for, in the words a person would use.
_WORK: dict[str, str] = {
    "INTAKE": "建立需求快照与协作绑定",
    "GRILL": "澄清歧义，产出决策日志与验收点（tom-grill）",
    "SPEC": "起草 Spec：行为、验收场景、测试接口、环境要求（tom-spec）",
    "TASKS": "把 Spec 拆成端到端任务 DAG（tom-tasks）",
    "WORKSPACE": "准备工作区与分支锁",
    "PLAN": "为当前任务写实现计划（tom-plan）",
    "IMPLEMENT": "生成业务改动与测试改动（tom-implement）",
    "REVIEW": "标准与 Spec 双轴 Review（tom-review）",
    "SUBMIT": "提交 iCode 并触发 iPipe",
    "IPIPE": "等待 iPipe 编译、单测、回归、集成证据",
    "DIAGNOSE": "定位失败并给出修复方向（tom-diagnose）",
    "RELEASE": "执行发布",
}

_BLOCKED_HINT: dict[str, str] = {
    "RECOVERY_REQUIRED": "有已发出但未确认的外部写入，先跑 resume 查明，再重试同一操作",
    "APPROVAL_REQUIRED": "门还没批，等如流回复或用 approve 落账",
    "TASK_FRONTIER_EMPTY": "没有可做的任务，回到 TASKS 检查 DAG",
    "PREDECESSOR_REQUIRED": "缺少上游阶段产物，先补齐前一阶段",
    "SOURCE_REVISION_REQUIRED": "缺少固定的源码版本，先固定基线",
    "PROFILE_CONFLICT": "项目 profile 被改过，需要新 run 或恢复原文件",
    "ACCEPTANCE_REQUIRED": "验收点为空，先和需求负责人定验收标准",
}


def _read_only_next(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """Ask the next oracle without recovery side effects.

    A few host integrations expose a minimal ``next(run_id)`` adapter rather than the
    full Orchestrator signature. Keep those read-only projections compatible while
    ensuring the real controller always receives ``read_only=True``.
    """
    next_fn = getattr(orchestrator, "next")
    try:
        parameters = inspect.signature(next_fn).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "read_only" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return next_fn(run_id, read_only=True)
    return next_fn(run_id)


def build(orchestrator: Any, run_id: str, *, include_progress: bool = True) -> dict[str, Any]:
    events = orchestrator.state.events(run_id)
    if not events:
        return {"run_id": run_id, "state": "RUN_NOT_FOUND", "waiting_on": [], "blocked": None}
    current = events[-1]
    terminal = current.get("state") in _TERMINAL_SUCCESS
    intake = events[0].get("payload") or {}
    # Status is observational. In particular, it must not recover a skipped SUBMIT
    # checkpoint merely because somebody refreshed the dashboard.
    action = _read_only_next(orchestrator, run_id)
    current_gate = action.get("required_human_gate") if action.get("ok") else None
    approvals = [] if terminal else orchestrator.approvals.for_run(run_id)
    # A PENDING card is superseded only when a *later* APPROVE of the same action
    # already exists. Counting any historical APPROVE hid the live G4 after
    # BGW-1956 re-entered PLAN, while still leaving the dead G2 e133d87e visible
    # until it was REJECT-closed.
    latest_approve_at: dict[str, str] = {}
    for row in approvals:
        if row.get("effective_decision") != "APPROVE":
            continue
        action_name = gate_of(row.get("action") or "")
        resolved = row.get("resolved_at") or ""
        if action_name and resolved >= latest_approve_at.get(action_name, ""):
            latest_approve_at[action_name] = resolved
    # A reissued approval keeps the same bare gate (for example G1#retry-1 is
    # still G1), but only the newest unresolved attempt is actionable.
    # After a revise/rewind the gates opened before it ask about an action the run
    # has left; listing them would send the reviewer to a card nothing can use.
    reentered_at = max(
        (event.get("created_at") or "" for event in events
         if isinstance((event.get("payload") or {}).get("reentry"), dict)),
        default="",
    )
    latest_unresolved: dict[str, dict[str, Any]] = {}
    for row in approvals:
        if row.get("effective_decision"):
            continue
        if (row.get("created_at") or "") < reentered_at and gate_of(row.get("action") or "") not in {
            "WORKFLOW_SPEC_MIGRATION", "PROFILE_REPIN",
        }:
            continue
        action_name = gate_of(row.get("action") or "")
        # A change request is about the run, not the phase's gate, and is the very
        # thing a person has to answer while the phase waits.
        if current_gate is not None and action_name not in {current_gate, "CHANGE_REQUEST"}:
            continue
        if action_name == "CHANGE_REQUEST" and not _change_request_live(orchestrator, run_id, row, current):
            continue
        created = row.get("created_at") or row.get("deadline_at") or ""
        if action_name in latest_approve_at and latest_approve_at[action_name] >= created:
            continue
        previous = latest_unresolved.get(action_name)
        previous_created = previous.get("created_at") if previous else ""
        if previous is None or created >= previous_created:
            latest_unresolved[action_name] = row
    open_gates = [
        {
            "action": row.get("action"),
            "approval_id": row.get("approval_id"),
            "input_hash": row.get("input_hash"),
            "deadline_at": row.get("deadline_at"),
        }
        for row in latest_unresolved.values()
    ]
    # A migration or change request decides what the phase gate is about (under drift
    # or a pending profile change its answer cannot even land), so it is named first.
    _first = {"WORKFLOW_SPEC_MIGRATION": 0, "CHANGE_REQUEST": 1}
    open_gates.sort(key=lambda gate: _first.get(gate_of(gate["action"] or ""), 2))
    pending = []
    side_writes: list[str] = []
    # Under evidence contract v2 an open KU/iCafe write is a side channel that blocks
    # nothing (recovery.resume skips it too); listing it as in flight told the operator
    # to settle a write the run does not wait on.
    side_channel = workflow_spec.run_evidence_contract(events) == "v2"
    if not terminal:
        for item in orchestrator.state.pending_intents(run_id):
            operation = str(item.get("operation") or "")
            from recovery import NOTICE_OPERATIONS

            if operation in NOTICE_OPERATIONS or (
                    side_channel and operation.split(".", 1)[0] in {"icafe", "ku", "knowledge"}):
                side_writes.append(operation)
                continue
            entry = {
                "operation": item.get("operation"),
                "intent_id": item.get("intent_id"),
            }
            if item.get("operation") == "approval.delivery.infoflow":
                payload = item.get("payload")
                if isinstance(payload, dict) and payload.get("approval_id"):
                    entry.update(
                        {
                            "recovery_action": "CONFIRM_APPROVAL_REDELIVERY",
                            "approval_id": payload["approval_id"],
                        }
                    )
            pending.append(entry)
    monitoring_reader = getattr(orchestrator.state, "ipipe_monitoring", None)
    monitoring = monitoring_reader(run_id) if callable(monitoring_reader) else []
    blocked = None if terminal or action.get("ok") else action.get("reason_code")
    # This is a presentation hint, not approval authorization. The completion
    # validator still checks the exact candidate hash against the ledger.
    approved = None if terminal else next((
        row for row in reversed(approvals)
        if current_gate and gate_of(row.get("action")) == current_gate
        and row.get("effective_decision") == "APPROVE"
        and row.get("created_at") and current.get("created_at")
        and row["created_at"] >= current["created_at"]
        and row.get("resolved_at")
    ), None)
    if not terminal and action.get("reason_code") == "WORKFLOW_SPEC_DRIFT":
        from workflow_spec_migration import ACTION, plan as migration_plan

        prepared = migration_plan(orchestrator, run_id)
        if prepared.get("ok"):
            approved = next((
                row for row in reversed(approvals)
                if row.get("run_id") == run_id
                and row.get("action") == ACTION
                and row.get("input_hash") == prepared.get("input_hash")
                and row.get("effective_decision") == "APPROVE"
                and row.get("resolved_at")
            ), approved)
    # A REJECT of the draft this attempt produced: the run waits on a person, not on
    # `continue`, which would only find the gate already answered.
    rejected = None if terminal or open_gates or approved else next((
        row for row in reversed(approvals)
        if current_gate and gate_of(row.get("action")) == current_gate
        and row.get("effective_decision") in {"REVISE", "REJECT"}
        and row.get("created_at") and current.get("created_at")
        and row["created_at"] >= current["created_at"]
    ), None)
    change_approved = None if terminal else next((
        row for row in reversed(approvals)
        if gate_of(row.get("action") or "") == "CHANGE_REQUEST"
        and row.get("effective_decision") == "APPROVE"
        and _change_request_live(orchestrator, run_id, row, current)
    ), None)
    if change_approved is not None:
        approved = change_approved
    producer_job = _producer_context(orchestrator, run_id, action, terminal)
    status_label = _status_label(current["state"], action, open_gates, pending, blocked, approved, producer_job, rejected)
    from progress_snapshot import build as build_progress
    next_step = _next_step(action, current["state"], open_gates, pending, blocked, approved, producer_job, rejected, events)
    progress = build_progress(orchestrator, run_id, next_step=next_step) if include_progress else None
    return {
        "run_id": run_id,
        "requirement_id": intake.get("requirement_id"),
        "project": intake.get("project"),
        "state": current["state"],
        "status_label": status_label,
        "since": current.get("created_at"),
        "phases_done": sum(1 for event in events[1:]),
        "waiting_on": open_gates,
        "in_flight": pending,
        "deferred_side_writes": sorted(set(side_writes)),
        "ipipe_monitoring": monitoring,
        "env_waits": [] if terminal else _env_waits(orchestrator, run_id),
        "blocked": blocked,
        "producer_job": producer_job,
        "progress": progress,
        # The gate this phase will have to pass, so a caller can tell an unanswered
        # gate from one that is already approved while the phase has not landed.
        "gate": action.get("required_human_gate"),
        "next": next_step,
    }


def _change_request_live(orchestrator: Any, run_id: str, row: dict[str, Any], current: dict[str, Any]) -> bool:
    """A change request still applies only where it was opened and until it is applied."""
    getter = getattr(getattr(orchestrator, "state", None), "idempotency_result", None)
    if not callable(getter):
        return False
    from run_change import snapshot

    prepared = snapshot(orchestrator.state, run_id, row.get("input_hash") or "")
    return (
        isinstance(prepared, dict)
        and prepared.get("from_event_id") == current.get("event_id")
        and getter(f"run-change:{row.get('approval_id')}") is None
    )


def _producer_context(orchestrator: Any, run_id: str, action: dict[str, Any], terminal: bool) -> dict[str, Any] | None:
    """Expose the current model task without leaking its draft or internal hashes."""
    if terminal or not action.get("action_id") or not action.get("child_skill"):
        return None
    state = getattr(orchestrator, "state", None)
    getter = getattr(state, "producer_job", None)
    if not callable(getter):
        return None
    job = getter(f"producer:{action['action_id']}")
    payload = job.get("payload") if isinstance(job, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    status = job.get("status") if isinstance(job, dict) else "PENDING"
    # A GRILL draft that still carries open questions is FULFILLED but cannot open
    # G1: `continue` would only fail again. Reporting it as "待审批" sent the operator
    # after an approval that is never created, so it is a state of its own.
    draft = job.get("draft") if isinstance(job, dict) else None
    draft = draft if isinstance(draft, dict) else {}
    awaiting_answers = status == "FULFILLED" and (
        draft.get("decision_result") == "UNRESOLVED" or bool(draft.get("choice_questions"))
    )
    draft_state = (
        "待答题" if awaiting_answers
        else ("已保存待审批" if action.get("required_human_gate") else "已保存待处理")
        if status == "FULFILLED"
        else "待生成"
    )
    return {
        "job_id": job.get("job_id") if isinstance(job, dict) else f"producer:{action['action_id']}",
        "phase": payload.get("phase") or action.get("phase"),
        "schema": action.get("result_schema") or payload.get("result_schema"),
        "status": status,
        "draft_state": draft_state,
        "awaiting_answers": awaiting_answers,
    }


def _env_waits(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    try:
        import env_wait

        return env_wait.open_waits(orchestrator.state, run_id)
    except Exception:  # noqa: BLE001 - status must render without the wait detail
        return []


def _next_step(
    action: dict[str, Any],
    state: str,
    open_gates: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    blocked: str | None,
    approved: dict[str, Any] | None = None,
    producer_job: dict[str, Any] | None = None,
    rejected: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Who acts next, and what exactly they do.

    An unresolved external write outranks an open gate: the approval cannot be
    safely acted on until the delivery lineage is settled or explicitly handed
    to controlled redelivery.
    """
    if state in {"RELEASE_SUCCESS", "STOPPED"}:
        return {"owner": "-", "text": "运行已结束，无后续动作"}
    if blocked == "RECOVERY_REQUIRED" or pending:
        redelivery_candidates = [
            item
            for item in pending
            if item.get("recovery_action") == "CONFIRM_APPROVAL_REDELIVERY"
        ]
        current_approval_ids = {
            item.get("approval_id") for item in open_gates if item.get("approval_id")
        }
        redelivery = next(
            (
                item for item in redelivery_candidates
                if item.get("approval_id") in current_approval_ids
            ),
            None,
        ) or (redelivery_candidates[-1] if redelivery_candidates else None)
        if redelivery:
            # `confirm-approval-redelivery` takes the intent id as a required
            # argument, so a status that hides it forces the operator to read the
            # state database by hand.
            intent = redelivery.get("intent_id") or "-"
            return {
                "owner": "你",
                "text": (
                    f"如流审批投递结果未知（审批 {redelivery['approval_id']}，intent {intent}）。"
                    "如果你已确认没有收到，请回复“未收到，允许重发”，"
                    "Agent 将绑定当前审批和 intent 做一次受控重发；不会创建新审批。"
                ),
            }
        operations = "、".join(sorted({item["operation"] for item in pending})) or "-"
        return {"owner": "Agent", "text": f"先收敛未确认的外部写入（{operations}），再继续"}
    # An approved change request replaces the phase gates still open, so applying it
    # comes before answering any of them.
    if approved and gate_of(approved.get("action") or "") == "CHANGE_REQUEST":
        return {
            "owner": "Agent",
            "text": f"变更请求已批准；执行 rewind --approval-id {approved.get('approval_id')} 回到目标阶段",
        }
    if approved and approved.get("action") == "WORKFLOW_SPEC_MIGRATION":
        return {
            "owner": "Agent",
            "text": "当前 workflow 迁移已批准待应用；调用 continue，由 worker 应用精确迁移后恢复",
        }
    if open_gates:
        gate = open_gates[0]
        approval_id = gate.get("approval_id") or "未知"
        return {
            "owner": "你",
            "text": (
                f"请查看已创建的如流审批卡（审批 ID {approval_id}），"
                f"核对材料后处理 {gate['action']} 门"
                f"（截止 {gate['deadline_at']}）；本消息不是审批"
            ),
        }
    # A phase's future gate is not an approval request.  Until the controller
    # has created a ledger row and delivered the exact hash, Comate still owns
    # the next step and the progress notice must not look like a prompt to
    # approve something that does not exist.
    if blocked == "APPROVAL_REQUIRED" and not open_gates:
        return {"owner": "Agent", "text": "审批记录尚未创建；Agent 先补齐本次审批材料并创建审批"}
    if state == "ARCHITECTURE_REVIEW":
        return {
            "owner": "你",
            "text": (
                "修复预算已用尽或同类失败反复出现，需要人决定回到哪个设计阶段："
                "告诉 agent 回到 GRILL / SPEC / TASKS 及原因，由它发起 rewind 变更请求审批"
            ),
        }
    if rejected:
        gate = gate_of(rejected.get("action") or "")
        approval_id = rejected.get("approval_id") or "-"
        if state in workflow_spec.REVISABLE_STATES:
            return {
                "owner": "你",
                "text": (
                    f"{gate} 已标记需修改（审批 {approval_id}）。把修改意见告诉 agent"
                    f"（`/tom-autodev <卡片> 需修改 <意见>`，或在如流点「需修改」后直接回复意见），"
                    f"Agent 会按意见重做草案并重新发起 {gate}；要回到更早阶段用 rewind，停止用 stop"
                ),
            }
        from run_change import rewind_available

        way_back = (
            "；如需改前面的设计，告诉 agent 回到 GRILL/SPEC/TASKS 中哪个阶段，由它发起 rewind"
            if rewind_available(events or [], state) else ""
        )
        return {
            "owner": "你",
            "text": (
                f"{gate} 已标记需修改（审批 {approval_id}），该门不会原地重发{way_back}；"
                "否则 stop 结束本运行"
            ),
        }
    if blocked:
        return {"owner": "你", "text": _BLOCKED_HINT.get(blocked, f"处理 {blocked}")}
    if approved:
        gate = gate_of(approved.get("action") or action.get("required_human_gate"))
        if gate == "G7" or state == "SUBMIT":
            return {
                "owner": "Agent",
                "text": f"{approved['action']} 已批准待提交；调用 continue，由 worker 执行提交并继续",
            }
        return {
            "owner": "Agent",
            "text": (
                f"{approved['action']} 已批准待提交；调用 continue，由 worker 复用已保存内容并执行下一步"
            ),
        }
    if producer_job and producer_job.get("awaiting_answers"):
        return {
            "owner": "你",
            "text": (
                f"决策日志仍有未答问题，尚未创建审批；先回答问题，Agent 再以同一 job "
                f"{producer_job.get('job_id')} 重新提交草案"
            ),
        }
    if producer_job and producer_job.get("status") == "FULFILLED":
        text = (
            "草案已保存；调用 continue 补发对应审批并等待审批结果"
            if action.get("required_human_gate")
            else "草案已保存；调用 continue，由控制器核验持久化结果并按阶段规则处理"
        )
        return {"owner": "Agent", "text": text}
    if action.get("child_skill") is not None:
        work = _WORK.get(state, state)
        skill = action.get("child_skill")
        return {
            "owner": "Agent",
            "text": f"{work}，当前为待生成；调用 {skill} 生成内容后提交",
        }
    work = _WORK.get(state, state)
    gate = action.get("required_human_gate")
    if gate:
        return {"owner": "Agent", "text": f"当前将进入 {gate}；审批记录尚未创建，继续处理材料"}
    if state == "IMPLEMENT":
        tail = "，当前为待生成；生成真实 change-set 后开 G5 门等你批"
    else:
        tail = f"，完成后开 {gate} 门等你批" if gate else ""
    return {"owner": "Agent", "text": f"{work}{tail}"}


def render(brief: dict[str, Any]) -> str:
    if brief.get("state") == "RUN_NOT_FOUND":
        return f"运行 {brief['run_id']} 不存在。"
    lines = []
    if brief.get("requirement_id") or brief.get("project"):
        lines.append(f"需求 {brief.get('requirement_id') or '-'} · 项目 {brief.get('project') or '-'}")
    lines.extend([
        f"运行 {brief['run_id'][:12]}… 当前阶段 {brief['state']}（{brief.get('status_label', '待处理')}）",
        f"最近一次状态变更 {brief.get('since') or '-'}",
    ])
    if brief["waiting_on"]:
        lines.append("等待审批：" + "、".join(
            f"{gate['action']}（截止 {gate['deadline_at']}）"
            for gate in brief["waiting_on"]
        ))
    else:
        lines.append("等待审批：无")
    if brief["in_flight"]:
        lines.append("未确认的外部写入：" + "、".join(
            sorted({item["operation"] for item in brief["in_flight"]})
        ))
    for wait in brief.get("env_waits") or []:
        lines.append(
            f"环境等待：stage {wait.get('stage_build_id')} · {wait.get('wait_reason_text')} · "
            f"待 {wait.get('next_actor')} · 下次复查 {wait.get('recheck_after')} · "
            f"最长等到 {wait.get('max_wait')}（已查 {wait.get('checks')} 次）")
    if brief.get("deferred_side_writes"):
        lines.append("延后补发的旁路写入（不阻断）：" + "、".join(brief["deferred_side_writes"]))
    for item in brief.get("ipipe_monitoring") or []:
        checkpoint = item.get("checkpoint") or {}
        stages = checkpoint.get("stages") or []
        active = next(
            (
                stage for stage in stages
                if stage.get("status") not in {"SUCCESS", "SUCC", "PASSED", "PASS"}
            ),
            None,
        )
        location = (
            f"{active.get('name') or active.get('stage_name') or active.get('stage_build_id')}"
            if isinstance(active, dict) else "阶段状态未知"
        )
        lines.append(
            f"iPipe 观察：build {item.get('build_id')} · "
            f"{checkpoint.get('status') or 'UNKNOWN'} · {location} · "
            f"最近检查 {item.get('updated_at') or '-'}"
        )
    if brief.get("blocked"):
        lines.append(f"阻塞原因 {brief['blocked']}")
    if brief.get("progress"):
        from progress_snapshot import render as render_progress
        lines.append(render_progress(brief["progress"]))
    producer = brief.get("producer_job")
    if producer:
        lines.append(
            f"模型任务：{producer.get('job_id')} · {producer.get('phase') or '-'} · "
            f"Schema {producer.get('schema') or '-'} · 草案{producer.get('draft_state') or '-'}"
        )
    step = brief["next"]
    if brief["waiting_on"] and not (brief["in_flight"] or brief.get("blocked")):
        # Only say "open the card" when the card's delivery is settled. An unknown
        # delivery or a blocked run keeps the derived recovery instruction, which
        # otherwise sent the operator after a card that never arrived.
        gates = "、".join(sorted({str(item.get("action") or "审批") for item in brief["waiting_on"]}))
        next_text = f"打开如流审批卡处理 {gates}"
    elif brief.get("status_label") == "已完成":
        next_text = "运行已结束，无后续动作"
    else:
        next_text = step["text"]
    lines.append(f"下一步（{step['owner']}）：{next_text}")
    return "\n".join(lines)
