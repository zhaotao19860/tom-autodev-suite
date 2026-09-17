"""The WorkerDriver decision core (Phase 1c, read-only skeleton).

`classify_next` is the pure "what should the durable worker do next for this run"
function — the *workflow-logic* layer in the activity-boundary annotation. It calls the
read-only `next` oracle and maps the action to one worker decision. It performs NO writes
and NO external side effects; the executor that actually completes phases, runs
controller transitions and posts approval cards is a separate, later layer that consumes
these decisions.

Decision kinds:
  - TERMINAL       : the run reached a terminal state; nothing to do.
  - BLOCKED        : `next` refused (recovery required / error); surface the reason.
  - AUTO_COMPLETE  : the action carries deterministic content and no gate (an express
                     auto phase, e.g. GRILL); the worker can complete it itself.
  - PRODUCER_WAIT  : the action needs model-authored content (a skill phase); park a
                     ProducerJob for a bounded agent turn (a signal-wait).
  - APPROVAL_WAIT  : the action needs a human gate that is not yet settled; park an
                     ApprovalJob (a signal-wait).
  - CONTROLLER_STEP: a controller action (workspace/submit/ipipe/release) whose gate is
                     settled (or ungated); the worker runs the deterministic transition
                     (a would-be Temporal activity).
"""

from __future__ import annotations

import copy
from typing import Any

import workflow_spec
from phase_protocol import _canonical_hash

TERMINAL = "TERMINAL"
BLOCKED = "BLOCKED"
AUTO_COMPLETE = "AUTO_COMPLETE"
PRODUCER_WAIT = "PRODUCER_WAIT"
APPROVAL_WAIT = "APPROVAL_WAIT"
CONTROLLER_STEP = "CONTROLLER_STEP"


def _gate_settled(orchestrator: Any, run_id: str, gate: str, input_hash: str | None) -> bool:
    """True when an APPROVE for `gate` bound to `input_hash` exists for the run."""
    for record in orchestrator.approvals.for_run(run_id):
        if (
            isinstance(record, dict)
            and record.get("action") == gate
            and record.get("effective_decision") == "APPROVE"
            and (input_hash is None or record.get("input_hash") == input_hash)
        ):
            return True
    return False


def classify_next(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """Read-only: the single worker decision for the run's current frontier."""
    action = orchestrator.next(run_id)
    if not action.get("ok"):
        return {"kind": BLOCKED, "reason_code": action.get("reason_code"), "action": action}

    state = action.get("state")
    if state in workflow_spec.terminal_states():
        return {"kind": TERMINAL, "state": state}

    gate = action.get("required_human_gate")

    # A deterministic auto phase: content pinned in the action, no gate.
    if action.get("content") is not None and gate is None and action.get("child_skill") is None:
        return {"kind": AUTO_COMPLETE, "action": action}

    # A skill phase needs model-authored content first; its output gate (if any) is
    # requested after production, so producing is the immediate next step.
    if action.get("child_skill") is not None:
        return {"kind": PRODUCER_WAIT, "skill": action["child_skill"], "action": action}

    # A controller phase: run the deterministic transition once its gate is settled.
    if gate is not None and not _gate_settled(orchestrator, run_id, gate, action.get("input_hash")):
        return {"kind": APPROVAL_WAIT, "gate": gate, "action": action}
    return {"kind": CONTROLLER_STEP, "controller": action.get("controller"), "action": action}


def build_envelope(action: dict[str, Any]) -> dict[str, Any]:
    """Build the ArtifactEnvelope for an auto/controller-authored action server-side.

    This is the "server builds the envelope from pinned content" seam: for an auto phase
    the content is already in the action, so the worker constructs the full envelope the
    same way the agent would for a no-gate phase (approval_id None), and `complete_phase`
    validates it unchanged. Only defined for auto actions (content present, no gate).
    """
    content = action["content"]
    content_hash = _canonical_hash(content)
    source_revisions = action.get("source_revisions")
    approval_input_hash = _canonical_hash({
        "action_id": action["action_id"],
        "task_id": action.get("task_id"),
        "parent_artifact_hash": action.get("parent_artifact_hash"),
        "source_revisions": source_revisions,
        "content_hash": content_hash,
    })
    return {
        "action_id": action["action_id"],
        "source_event_id": action["source_event_id"],
        "host": "comate",
        "run_id": action["run_id"],
        "phase": action["phase"],
        "task_id": action.get("task_id"),
        "schema_version": "1",
        "input_hash": action["input_hash"],
        "content_hash": content_hash,
        "source_revisions": source_revisions,
        "parent_artifact_hash": action.get("parent_artifact_hash"),
        "knowledge_doc_id": None,
        "knowledge_url": None,
        "knowledge_version": None,
        "icafe_comment_id": None,
        "evidence_refs": copy.deepcopy(action.get("source_evidence_refs") or []),
        "approval_id": None,
        "approval_input_hash": approval_input_hash,
        "content": content,
    }


def execute_auto(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None) -> dict[str, Any]:
    """Complete one AUTO_COMPLETE phase without an agent turn.

    The smallest executor step: only a deterministic, ungated auto phase (e.g. express
    GRILL) is completed here — the content is pinned in the action, so there is no model
    call and no approval. Any other frontier is refused (NOT_AUTO), so the worker can
    never use this path to perform a gated or model-authored step.
    """
    decision = classify_next(orchestrator, run_id)
    if decision["kind"] != AUTO_COMPLETE:
        return {"ok": False, "reason_code": "NOT_AUTO", "decision_kind": decision["kind"]}
    envelope = build_envelope(decision["action"])
    return orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge_sync)


# States a deterministic auto-advance may complete on its own. Controller side effects
# (submit / iPipe / release) are deliberately NOT auto-run here: they touch external
# systems and each keeps its own gate, so the loop parks on them for an explicit step.
def advance(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
            max_steps: int = 32) -> dict[str, Any]:
    """Drive a run forward through deterministic auto phases, then park.

    Loops: complete every AUTO_COMPLETE frontier itself (no agent, no gate), and stop as
    soon as the frontier needs the model (PRODUCER_WAIT), a human gate (APPROVAL_WAIT), a
    controller side effect (CONTROLLER_STEP), reaches a terminal state (TERMINAL), or is
    blocked (BLOCKED). The returned `parked` decision says what the caller must arrange
    next. `max_steps` bounds the loop against a mis-specced auto cycle.

    This is the safe half of the driver: it never autonomously submits to iCode, triggers
    iPipe, or releases — those CONTROLLER_STEP decisions are surfaced, not executed.
    """
    steps: list[dict[str, Any]] = []
    for _ in range(max_steps):
        decision = classify_next(orchestrator, run_id)
        if decision["kind"] == PRODUCER_WAIT:
            # Durably record what the model must produce, so parking is actionable and
            # idempotent: the agent (or a headless producer) fulfils this job later via
            # submit-draft. Enqueuing is a local, idempotent write — no external effect.
            job = _enqueue_producer_job(orchestrator, run_id, decision)
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": PRODUCER_WAIT, "producer_job": job, "decision": decision,
                    "auto_completed": steps}
        if decision["kind"] != AUTO_COMPLETE:
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": decision["kind"], "decision": decision, "auto_completed": steps}
        result = execute_auto(orchestrator, run_id, knowledge_sync=knowledge_sync)
        if not result.get("ok"):
            return {"ok": False, "reason_code": "AUTO_COMPLETE_FAILED", "run_id": run_id,
                    "detail": result, "auto_completed": steps}
        steps.append({"phase": decision["action"].get("phase"), "result": result})
    return {"ok": False, "reason_code": "MAX_STEPS_EXCEEDED", "run_id": run_id,
            "auto_completed": steps}


def _enqueue_producer_job(orchestrator: Any, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """Record a ProducerJob for a PRODUCER_WAIT frontier (idempotent on the action id)."""
    action = decision["action"]
    job_id = f"producer:{action['action_id']}"
    payload = {
        "phase": action.get("phase"),
        "skill": decision.get("skill"),
        "result_schema": action.get("result_schema"),
        "input_hash": action.get("input_hash"),
        "action_id": action.get("action_id"),
        "source_event_id": action.get("source_event_id"),
        "task_id": action.get("task_id"),
        "required_human_gate": action.get("required_human_gate"),
    }
    return orchestrator.state.record_producer_job(run_id, job_id, payload)
