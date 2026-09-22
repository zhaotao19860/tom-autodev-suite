"""Answer three questions about a run without reading a JSON event dump.

Where is it, is anything in flight, and what does the next step need from whom.
The same lines are printed in the IDE and pushed to 如流, so an operator never has
to reconstruct state from `status` output or guess whether "继续" is even the right
thing to say.
"""
from __future__ import annotations

from typing import Any

from approval_ledger import gate_of

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


def build(orchestrator: Any, run_id: str) -> dict[str, Any]:
    events = orchestrator.state.events(run_id)
    if not events:
        return {"run_id": run_id, "state": "RUN_NOT_FOUND", "waiting_on": [], "blocked": None}
    current = events[-1]
    action = orchestrator.next(run_id)
    current_gate = action.get("required_human_gate") if action.get("ok") else None
    approvals = orchestrator.approvals.for_run(run_id)
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
    open_gates = []
    for row in approvals:
        if row.get("effective_decision"):
            continue
        action_name = gate_of(row.get("action") or "")
        if current_gate is not None and action_name != current_gate:
            continue
        created = row.get("created_at") or row.get("deadline_at") or ""
        if action_name in latest_approve_at and latest_approve_at[action_name] >= created:
            continue
        open_gates.append(
            {
                "action": row.get("action"),
                "approval_id": row.get("approval_id"),
                "input_hash": row.get("input_hash"),
                "deadline_at": row.get("deadline_at"),
            }
        )
    pending = [
        {"operation": item.get("operation"), "intent_id": item.get("intent_id")}
        for item in orchestrator.state.pending_intents(run_id)
    ]
    blocked = None if action.get("ok") else action.get("reason_code")
    # This is a presentation hint, not approval authorization. The completion
    # validator still checks the exact candidate hash against the ledger.
    approved = next((
        row for row in reversed(approvals)
        if current_gate and gate_of(row.get("action")) == current_gate
        and row.get("effective_decision") == "APPROVE"
        and row.get("created_at") and current.get("created_at")
        and row["created_at"] >= current["created_at"]
        and row.get("resolved_at")
    ), None)
    return {
        "run_id": run_id,
        "state": current["state"],
        "since": current.get("created_at"),
        "phases_done": sum(1 for event in events[1:]),
        "waiting_on": open_gates,
        "in_flight": pending,
        "blocked": blocked,
        # The gate this phase will have to pass, so a caller can tell an unanswered
        # gate from one that is already approved while the phase has not landed.
        "gate": action.get("required_human_gate"),
        "next": _next_step(action, current["state"], open_gates, pending, blocked, approved),
    }


def _next_step(
    action: dict[str, Any],
    state: str,
    open_gates: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    blocked: str | None,
    approved: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Who acts next, and what exactly they do.

    An open gate outranks everything else: the phase cannot land until it is
    answered, so telling the operator to keep working would be wrong.
    """
    if open_gates:
        gate = open_gates[0]
        return {
            "owner": "你",
            "text": (
                f"在如流回复 APPROVE {gate['approval_id']} 或 REJECT {gate['approval_id']}"
                f"（{gate['action']} 门，截止 {gate['deadline_at']}）"
            ),
        }
    if blocked == "RECOVERY_REQUIRED" or pending:
        operations = "、".join(sorted({item["operation"] for item in pending})) or "-"
        return {"owner": "Comate", "text": f"先收敛未确认的外部写入（{operations}），再继续"}
    if blocked:
        return {"owner": "你", "text": _BLOCKED_HINT.get(blocked, f"处理 {blocked}")}
    if approved:
        gate = gate_of(approved.get("action") or action.get("required_human_gate"))
        if gate == "G7" or state == "SUBMIT":
            return {
                "owner": "Comate",
                "text": (
                    f"{approved['action']} 已批准待提交：核验审批 "
                    f"{approved['approval_id']} 与 submit descriptor 的 input_hash 完全一致后 "
                    "执行 cli.py submit（iCode 提交，不是 complete-phase）；"
                    "缺失或不匹配则阻塞，不要重复 resume 或开同一审批"
                ),
            }
        return {
            "owner": "Comate",
            "text": (
                f"{approved['action']} 已批准待提交：读取已保存的真实产物，核验审批 "
                f"{approved['approval_id']} 与候选 approval_input_hash 完全一致后 "
                "complete-phase（不是 iCode 提交）；缺失或不匹配则阻塞，"
                "不要重复 resume、生成产物或开同一审批"
            ),
        }
    work = _WORK.get(state, state)
    gate = action.get("required_human_gate")
    if state == "IMPLEMENT":
        tail = "，当前为待生成；生成真实 change-set 后开 G5 门等你批"
    else:
        tail = f"，完成后开 {gate} 门等你批" if gate else ""
    return {"owner": "Comate", "text": f"{work}{tail}"}


def render(brief: dict[str, Any]) -> str:
    if brief.get("state") == "RUN_NOT_FOUND":
        return f"运行 {brief['run_id']} 不存在。"
    lines = [
        f"运行 {brief['run_id'][:12]}… 当前阶段 {brief['state']}",
        f"最近一次状态变更 {brief.get('since') or '-'}",
    ]
    if brief["waiting_on"]:
        lines.append("等待审批：" + "、".join(
            f"{gate['action']}（{gate['approval_id'][:8]}…，截止 {gate['deadline_at']}）"
            for gate in brief["waiting_on"]
        ))
    else:
        lines.append("等待审批：无")
    if brief["in_flight"]:
        lines.append("未确认的外部写入：" + "、".join(
            sorted({item["operation"] for item in brief["in_flight"]})
        ))
    if brief.get("blocked"):
        lines.append(f"阻塞原因 {brief['blocked']}")
    step = brief["next"]
    lines.append(f"下一步（{step['owner']}）：{step['text']}")
    return "\n".join(lines)
