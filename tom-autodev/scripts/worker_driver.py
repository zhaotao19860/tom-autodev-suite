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
import subprocess
from pathlib import Path
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


def build_envelope(
    action: dict[str, Any], content: dict[str, Any] | None = None, approval_id: str | None = None
) -> dict[str, Any]:
    """Build the ArtifactEnvelope for an action server-side from its content.

    The "server builds the envelope" seam, shared by execute_auto (auto phase, content
    pinned in the action, no approval) and submit_draft (model phase, content is the
    producer's DraftContent, approval bound). `complete_phase` validates it unchanged.
    """
    content = action["content"] if content is None else content
    content_hash = _canonical_hash(content)
    source_revisions = (
        content.get("revisions") if action.get("phase") == "IMPLEMENT"
        else action.get("source_revisions")
    )
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
        "approval_id": approval_id,
        "approval_input_hash": approval_input_hash,
        "content": content,
    }


def _settled_approval_id(orchestrator: Any, run_id: str, gate: str, input_hash: str) -> str | None:
    for record in orchestrator.approvals.for_run(run_id):
        if (
            isinstance(record, dict)
            and record.get("action") == gate
            and record.get("effective_decision") == "APPROVE"
            and record.get("input_hash") == input_hash
        ):
            return record.get("approval_id")
    return None


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


def submit_draft(
    orchestrator: Any, run_id: str, job_id: str, draft: dict[str, Any],
    knowledge_sync: Any | None = None,
) -> dict[str, Any]:
    """Fulfil a ProducerJob with model-authored DraftContent and complete the phase.

    This is the producer-return seam: the agent (or a headless producer) supplies the
    DraftContent for the parked model phase; the server builds the envelope and completes.
    The job must match the current frontier (stale jobs are refused). For a gated phase
    the completion binds the settled approval for the draft's content hash; if no such
    approval exists yet the draft is recorded and APPROVAL_REQUIRED is returned with the
    exact hash to approve — so a model phase is never completed without its human gate.
    """
    decision = classify_next(orchestrator, run_id)
    if decision["kind"] != PRODUCER_WAIT:
        return {"ok": False, "reason_code": "NOT_PRODUCER", "decision_kind": decision["kind"]}
    action = decision["action"]
    expected_job = f"producer:{action['action_id']}"
    if job_id != expected_job:
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB", "expected_job_id": expected_job}

    change_class = workflow_spec.change_class_of(orchestrator.state.events(run_id))
    if workflow_spec.phase_mode(change_class, action["phase"]) == "merged":
        return _submit_merged(orchestrator, run_id, job_id, action, draft, knowledge_sync)

    orchestrator.state.fulfill_producer_job(job_id, draft)
    envelope = build_envelope(action, draft)
    gate = action.get("required_human_gate")
    if gate is not None:
        approval_id = _settled_approval_id(orchestrator, run_id, gate, envelope["approval_input_hash"])
        if approval_id is None:
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": gate,
                    "approval_input_hash": envelope["approval_input_hash"], "job_id": job_id}
        envelope["approval_id"] = approval_id
    return orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge_sync)


def _submit_merged(
    orchestrator: Any, run_id: str, job_id: str, action: dict[str, Any],
    draft: dict[str, Any], knowledge_sync: Any | None,
) -> dict[str, Any]:
    """Complete a merged design front (SPEC + TASKS) from one {spec, dag} DraftContent.

    SPEC is gated (its own gate binds the spec content); TASKS is ungated (the owner's
    express declaration covers it). Both artifacts are still written and validated against
    their own schemas, so everything downstream reads a normal spec and a normal task-dag.
    """
    if not (isinstance(draft, dict) and isinstance(draft.get("spec"), dict) and isinstance(draft.get("dag"), dict)):
        return {"ok": False, "reason_code": "MERGED_DRAFT_INVALID", "job_id": job_id}
    orchestrator.state.fulfill_producer_job(job_id, draft)

    spec_envelope = build_envelope(action, draft["spec"])
    gate = action.get("required_human_gate")
    if gate is not None:
        approval_id = _settled_approval_id(orchestrator, run_id, gate, spec_envelope["approval_input_hash"])
        if approval_id is None:
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": gate,
                    "approval_input_hash": spec_envelope["approval_input_hash"], "job_id": job_id}
        spec_envelope["approval_id"] = approval_id
    spec_result = orchestrator.complete_phase(run_id, spec_envelope, knowledge_sync=knowledge_sync)
    if not spec_result.get("ok"):
        return {"ok": False, "reason_code": "MERGED_SPEC_FAILED", "detail": spec_result}

    tasks_action = orchestrator.next(run_id)
    if not tasks_action.get("ok") or tasks_action.get("phase") != "TASKS":
        return {"ok": False, "reason_code": "MERGED_TASKS_UNAVAILABLE", "detail": tasks_action}
    tasks_envelope = build_envelope(tasks_action, draft["dag"])
    tasks_result = orchestrator.complete_phase(run_id, tasks_envelope, knowledge_sync=knowledge_sync)
    if not tasks_result.get("ok"):
        return {"ok": False, "reason_code": "MERGED_TASKS_FAILED", "detail": tasks_result}
    return {"ok": True, "reason_code": "MERGED_COMPLETE", "run_id": run_id,
            "spec": spec_result, "tasks": tasks_result}


def execute_controller(
    orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
    icode_skill: str = "/Users/tom/.comate/skills/.system/icode",
    icode_runtime: Any | None = None,
) -> dict[str, Any]:
    """Execute the current controller transition, if it is one the worker owns.

    WORKSPACE (local, reversible worktree binding + gated WORKSPACE->PLAN) and SUBMIT
    (derive the reviewed descriptor and submit it to iCode under G7) are executed here.
    IPIPE (triggers a pipeline) and RELEASE still return CONTROLLER_NEEDS_RUNTIME and are
    driven by their dedicated runtime-injected paths. Every path goes through the
    orchestrator's own gate enforcement, so nothing performs an un-approved action; SUBMIT
    in particular only opens/updates a CR once G7 is APPROVE for that exact descriptor.
    """
    decision = classify_next(orchestrator, run_id)
    if decision["kind"] not in (CONTROLLER_STEP, APPROVAL_WAIT):
        return {"ok": False, "reason_code": "NOT_CONTROLLER", "decision_kind": decision["kind"]}
    action = decision.get("action") or {}
    controller = action.get("controller")
    if controller == "workspace":
        return _execute_workspace(orchestrator, run_id, action)
    if controller == "submit":
        from submit_descriptor import build_and_archive
        from orchestrator import _submit, _latest_unsubmitted_reviewed_task

        task_id = action.get("task_id") or _latest_unsubmitted_reviewed_task(orchestrator, run_id)
        if not task_id:
            return {"ok": False, "reason_code": "SUBMIT_FRONTIER_EMPTY", "run_id": run_id}
        # Derive the reviewed descriptor (idempotent) to learn its input_hash, so G7 can be
        # approved for the exact submission before iCode is ever touched. Only once G7 is
        # APPROVE for that hash does the worker submit.
        built = build_and_archive(orchestrator, run_id, task_id)
        if not built.get("ok"):
            return {"ok": False, "reason_code": "SUBMIT_DESCRIPTOR_UNAVAILABLE", "detail": built}
        input_hash = built["descriptor"]["input_hash"]
        approval_id = _settled_approval_id(orchestrator, run_id, "G7", input_hash)
        if approval_id is None:
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G7",
                    "approval_input_hash": input_hash, "task_id": task_id}
        return _submit(orchestrator, run_id, task_id, approval_id, icode_skill, icode_runtime=icode_runtime)
    return {"ok": False, "reason_code": "CONTROLLER_NEEDS_RUNTIME", "controller": controller}


def _execute_workspace(orchestrator: Any, run_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """Create the task's owned worktrees, then advance WORKSPACE->PLAN once G4 is settled.

    The WORKSPACE gate binds the *binding* hash, which is only known after the worktrees
    are cut — so this does the deterministic, local prep (worktree creation is reversible)
    and then, like submit_draft, returns APPROVAL_REQUIRED with the exact hash to approve
    if G4 is not yet settled for it.
    """
    task_id = action.get("task_id")
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    selected = {"business": (profile.get("business_repos") or [None])[0], "tests": profile.get("test_repo")}
    if not all(isinstance(repository, dict) and repository.get("path") for repository in selected.values()):
        return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
    receipts: dict[str, Any] = {}
    from submit_descriptor import _ownership_rows, owned_row

    for role, repository in selected.items():
        repo = Path(repository["path"])
        try:
            revision = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            return {"ok": False, "reason_code": "WORKSPACE_BASELINE_UNVERIFIED", "detail": str(error)}
        baseline = orchestrator.workspaces.inspect(repo, run_id, task_id, baseline_evidence={"revision": revision})
        created = orchestrator.workspaces.create(repo, run_id, task_id, baseline)
        if created.get("status") == "CREATED":
            reservation = created
        elif created.get("reason_code") == "WORKTREE_ALREADY_RESERVED":
            # This is the second pass of the compute-then-approve flow: the worktrees are
            # already cut, so reuse the recorded reservation. Identical receipts keep the
            # binding hash stable, so the G4 approval still matches.
            reservation = owned_row(_ownership_rows(orchestrator, run_id), task_id, repository["path"])
            if not isinstance(reservation, dict):
                return {"ok": False, "reason_code": "WORKSPACE_CREATE_FAILED", "role": role, "detail": created}
        else:
            return {"ok": False, "reason_code": "WORKSPACE_CREATE_FAILED", "role": role, "detail": created}
        receipts[role] = {
            "role": role, "module": repository.get("module"), "repo_path": str(repo.resolve()),
            "worktree_path": reservation["worktree_path"],
            "baseline_revision": reservation["baseline_revision"],
            "owner_token": reservation["owner_token"],
        }
    binding = orchestrator.workspace_binding(run_id, receipts)
    if not binding.get("ok"):
        return binding
    gate_hash = binding["input_hash"]
    approval_id = _settled_approval_id(orchestrator, run_id, "G4", gate_hash)
    if approval_id is None:
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G4",
                "approval_input_hash": gate_hash, "workspace_receipts": receipts}
    workspace_binding = binding["workspace_binding"]
    revisions = workspace_binding["source_revisions"]
    result = orchestrator.advance(run_id, "PLAN", {
        "input_hash": gate_hash, "approval_id": approval_id,
        "artifacts": ["workspace", "task-plan"], "workspace_receipts": receipts,
        "task_id": workspace_binding["task_id"], "source_revisions": revisions,
        "repo_revisions": revisions, "evidence_revisions": revisions,
    })
    landed = orchestrator.status(run_id).get("state")
    if landed != "PLAN":
        return {"ok": False, "reason_code": "CONTROLLER_ADVANCE_FAILED", "state": landed, "detail": result}
    return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id, "state": "PLAN", "result": result}


# States a deterministic auto-advance may complete on its own. Controller side effects
# (submit / iPipe / release) are deliberately NOT auto-run here: they touch external
# systems and each keeps its own gate, so the loop parks on them for an explicit step.
def advance(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
            max_steps: int = 32, icode_skill: str = "/Users/tom/.comate/skills/.system/icode",
            icode_runtime: Any | None = None) -> dict[str, Any]:
    """Drive a run forward through every step the worker can take on its own, then park.

    Loops: completes AUTO_COMPLETE frontiers, and executes the WORKSPACE and SUBMIT
    controllers when their gate is already settled (the compute-then-approve executors
    park with the exact hash when it is not). It parks — returning the decision the caller
    must arrange next — on a model phase (PRODUCER_WAIT, enqueuing a ProducerJob), an
    unsettled controller gate, an IPIPE/RELEASE controller (needs its runtime), a terminal
    state, or a block. `max_steps` bounds the loop.

    Only WORKSPACE (local) and SUBMIT (G7-gated CR) are ever executed here; IPIPE and
    RELEASE are surfaced, not run. Every controller execution goes through the
    orchestrator's own gate enforcement.
    """
    steps: list[dict[str, Any]] = []
    for _ in range(max_steps):
        decision = classify_next(orchestrator, run_id)
        kind = decision["kind"]
        if kind == AUTO_COMPLETE:
            result = execute_auto(orchestrator, run_id, knowledge_sync=knowledge_sync)
            if not result.get("ok"):
                return {"ok": False, "reason_code": "AUTO_COMPLETE_FAILED", "run_id": run_id,
                        "detail": result, "auto_completed": steps}
            steps.append({"phase": decision["action"].get("phase"), "result": result})
            continue
        if kind == PRODUCER_WAIT:
            job = _enqueue_producer_job(orchestrator, run_id, decision)
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": PRODUCER_WAIT, "producer_job": job, "decision": decision,
                    "auto_completed": steps}
        if kind in (CONTROLLER_STEP, APPROVAL_WAIT):
            controller = (decision.get("action") or {}).get("controller")
            if controller in ("workspace", "submit"):
                result = execute_controller(
                    orchestrator, run_id, knowledge_sync=knowledge_sync,
                    icode_skill=icode_skill, icode_runtime=icode_runtime,
                )
                if result.get("ok"):
                    steps.append({"controller": controller, "result": result})
                    continue
                if result.get("reason_code") == "APPROVAL_REQUIRED":
                    return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                            "parked": APPROVAL_WAIT, "controller": controller,
                            "gate": result.get("gate"),
                            "approval_input_hash": result.get("approval_input_hash"),
                            "auto_completed": steps}
                return {"ok": False, "reason_code": "CONTROLLER_STEP_FAILED", "run_id": run_id,
                        "controller": controller, "detail": result, "auto_completed": steps}
            # intake / ipipe / release: not executed by the worker loop.
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": kind, "controller": controller, "decision": decision,
                    "auto_completed": steps}
        # TERMINAL / BLOCKED
        return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                "parked": kind, "decision": decision, "auto_completed": steps}
    return {"ok": False, "reason_code": "MAX_STEPS_EXCEEDED", "run_id": run_id,
            "auto_completed": steps}


def _enqueue_producer_job(orchestrator: Any, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """Record a ProducerJob for a PRODUCER_WAIT frontier (idempotent on the action id)."""
    action = decision["action"]
    job_id = f"producer:{action['action_id']}"
    payload = {
        "phase": action.get("phase"),
        "skill": decision.get("skill"),
        "mode": workflow_spec.phase_mode(
            workflow_spec.change_class_of(orchestrator.state.events(run_id)), action.get("phase")
        ),
        "result_schema": action.get("result_schema"),
        "input_hash": action.get("input_hash"),
        "action_id": action.get("action_id"),
        "source_event_id": action.get("source_event_id"),
        "task_id": action.get("task_id"),
        "required_human_gate": action.get("required_human_gate"),
    }
    return orchestrator.state.record_producer_job(run_id, job_id, payload)
