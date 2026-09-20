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
import logging
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import workflow_spec
from phase_protocol import _canonical_hash
from schema_validator import validate_named_schema

_log = logging.getLogger(__name__)

# Wall-clock bound on how long the worker will monitor a triggered pipeline before it
# parks for a human. The runtime's own max_polls/poll_interval bound the poll count; this
# bounds elapsed time so a stuck build surfaces as a park rather than blocking the loop.
_IPIPE_MONITOR_WINDOW = timedelta(hours=2)

# One worker drives a given run at a time. The lease key is per-run; a crashed holder's
# lease goes stale (TTL elapsed + dead pid) and the next worker takes it over, resuming
# from the last committed event — every side effect is already idempotency-keyed, so the
# takeover cannot double-apply. The TTL only bounds how long a *crashed* holder blocks a
# takeover; a live worker releases the lease as soon as its drive returns.
_WORKER_LEASE_PREFIX = "worker-run:"
_WORKER_LEASE_TTL_SECONDS = 300

TERMINAL = "TERMINAL"
BLOCKED = "BLOCKED"
AUTO_COMPLETE = "AUTO_COMPLETE"
PRODUCER_WAIT = "PRODUCER_WAIT"
APPROVAL_WAIT = "APPROVAL_WAIT"
CONTROLLER_STEP = "CONTROLLER_STEP"


def _gate_settled(orchestrator: Any, run_id: str, gate: str, input_hash: str | None) -> bool:
    """True when an APPROVE for `gate` bound to exactly `input_hash` exists for the run.

    A missing/None `input_hash` never matches: a gate can only be settled against a concrete
    hash, so it fails closed rather than matching any approval for that gate.
    """
    if not isinstance(input_hash, str) or not input_hash:
        return False
    for record in orchestrator.approvals.for_run(run_id):
        if (
            isinstance(record, dict)
            and record.get("action") == gate
            and record.get("effective_decision") == "APPROVE"
            and record.get("input_hash") == input_hash
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


def _record_model_receipt(
    orchestrator: Any, run_id: str, action: dict[str, Any], draft: dict[str, Any],
    validators: list[Any],
) -> None:
    """Record how a ProducerJob's DraftContent was produced, for replay and cross-model
    diffing. The agent-turn backend knows no provider/model/temperature/seed, so those are
    null; the load-bearing fields are the pinned input_hash, the draft's output_hash, the
    prompt/spec versions, and which schema validators the draft is about to be checked
    against. Best-effort: a receipt failure must never block completing the phase."""
    try:
        orchestrator.state.record_model_execution_receipt(run_id, {
            "job_id": f"producer:{action['action_id']}",
            "action_id": action.get("action_id"),
            "phase": action.get("phase"),
            "backend": "agent-turn",
            "provider": None, "model": None, "model_version": None,
            "temperature": None, "seed": None,
            "prompt_version": workflow_spec.WORKFLOW_VERSION,
            "workflow_spec_hash": workflow_spec.canonical_hash(),
            "skill_version": action.get("child_skill"),
            "input_hash": action.get("input_hash"),
            "output_hash": _canonical_hash(draft),
            "validators_passed": [name for name in validators if name],
        })
    except Exception:
        _log.warning("model-execution-receipt write failed for %s (best-effort)",
                     action.get("action_id"), exc_info=True)
    # Cache the draft under (input_hash, prompt_version, model) so a worker re-driving the
    # same frontier reuses it instead of re-invoking the producer. Best-effort.
    try:
        orchestrator.state.cache_draft(
            action.get("input_hash"), workflow_spec.WORKFLOW_VERSION, None, draft
        )
    except Exception:
        _log.warning("draft-cache write failed for %s (best-effort)",
                     action.get("input_hash"), exc_info=True)


def cached_draft_for(orchestrator: Any, action: dict[str, Any]) -> dict[str, Any] | None:
    """The cached DraftContent for this frontier's (input_hash, prompt_version, model), if a
    producer already filled an identical frontier — the reuse seam for a re-driven worker."""
    try:
        cached = orchestrator.state.cached_draft(
            action.get("input_hash"), workflow_spec.WORKFLOW_VERSION, None
        )
    except Exception:
        _log.debug("draft-cache lookup failed for %s", action.get("input_hash"), exc_info=True)
        return None
    return cached["draft"] if isinstance(cached, dict) else None


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
    Once a draft is recorded, the worker can finish the phase from it on the next advance()
    without re-invoking the producer (see `_drive`), so a post-approval `resume()` alone
    completes the phase.
    """
    decision = classify_next(orchestrator, run_id)
    if decision["kind"] != PRODUCER_WAIT:
        return {"ok": False, "reason_code": "NOT_PRODUCER", "decision_kind": decision["kind"]}
    action = decision["action"]
    expected_job = f"producer:{action['action_id']}"
    if job_id != expected_job:
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB", "expected_job_id": expected_job}
    return _produce(orchestrator, run_id, job_id, action, draft, knowledge_sync)


def _produce(
    orchestrator: Any, run_id: str, job_id: str, action: dict[str, Any],
    draft: dict[str, Any], knowledge_sync: Any | None,
) -> dict[str, Any]:
    """Validate a DraftContent, persist it, and complete the phase (or park on its gate).

    Shared by submit_draft (caller supplies the draft) and the worker's auto-consume path
    (draft read back from the persisted ProducerJob). fulfill/receipt are idempotent, so
    completing from a persisted draft after approval re-runs this safely.
    """
    change_class = workflow_spec.change_class_of(orchestrator.state.events(run_id))
    if workflow_spec.phase_mode(change_class, action["phase"]) == "merged":
        return _submit_merged(orchestrator, run_id, job_id, action, draft, knowledge_sync)

    envelope = build_envelope(action, draft)
    # Validate the draft BEFORE locking the ProducerJob to it (HIGH-002): a schema-invalid
    # draft leaves the job PENDING so a corrected draft can retry the same frontier, instead
    # of locking it to a bad draft that every later submit conflicts with. The receipt's
    # validators_passed then reflects a check that actually ran.
    schema_name = action.get("result_schema")
    issues = validate_named_schema(draft, schema_name) if schema_name else []
    if issues:
        return {"ok": False, "reason_code": "DRAFT_SCHEMA_INVALID", "retry_allowed": True,
                "job_id": job_id, "schema": schema_name,
                "schema_errors": [{"path": i.path, "kind": i.kind} for i in issues]}
    orchestrator.state.fulfill_producer_job(job_id, draft)
    _record_model_receipt(orchestrator, run_id, action, draft, [schema_name])
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
    # Validate both halves BEFORE locking the job (HIGH-002): a schema-invalid spec or dag
    # leaves the merged job PENDING for a corrected retry rather than locking a bad bundle.
    issues = validate_named_schema(draft["spec"], "spec") + validate_named_schema(draft["dag"], "task-dag")
    if issues:
        return {"ok": False, "reason_code": "DRAFT_SCHEMA_INVALID", "retry_allowed": True,
                "job_id": job_id,
                "schema_errors": [{"path": i.path, "kind": i.kind} for i in issues]}
    orchestrator.state.fulfill_producer_job(job_id, draft)
    _record_model_receipt(orchestrator, run_id, action, draft, ["spec", "task-dag"])

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
    # Checkpoint the DAG under the TASKS ProducerJob before completing it (MEDIUM-002): if
    # the process dies after SPEC commits but before TASKS, resume() finds this FULFILLED
    # TASKS job and auto-completes it from the persisted DAG (HIGH-001), instead of leaving
    # SPEC done + TASKS orphaned and re-invoking the producer for the bundle.
    _enqueue_producer_job(
        orchestrator, run_id, {"action": tasks_action, "skill": tasks_action.get("child_skill")})
    orchestrator.state.fulfill_producer_job(f"producer:{tasks_action['action_id']}", draft["dag"])
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
    ipipe_api: Any | None = None,
) -> dict[str, Any]:
    """Execute the current controller transition, if it is one the worker owns.

    WORKSPACE (local, reversible worktree binding + gated WORKSPACE->PLAN) and SUBMIT
    (derive the reviewed descriptor and submit it to iCode under G7) are executed here.
    IPIPE (trigger the pipeline under G7, monitor to a terminal state, ingest the evidence)
    and RELEASE (verify the release under G9, record the evidence, land RELEASE_SUCCESS) are
    executed too once their `ipipe_api` transport is injected — the same runtime backs both.
    The G7 approval that authorized the submit authorizes the trigger, computed-then-approved
    here so the executed trigger hash cannot drift from what was approved; RELEASE runs only
    once G9 is APPROVE for that exact release action. Every path goes through the
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
    if controller == "ipipe" and ipipe_api is not None:
        return _execute_ipipe(orchestrator, run_id, action, ipipe_api, knowledge_sync=knowledge_sync)
    if controller == "release" and ipipe_api is not None:
        return _execute_release(orchestrator, run_id, action, ipipe_api, knowledge_sync=knowledge_sync)
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


def _execute_ipipe(
    orchestrator: Any, run_id: str, action: dict[str, Any], ipipe_api: Any,
    knowledge_sync: Any | None = None,
) -> dict[str, Any]:
    """Trigger the pipeline under G7, monitor it, and ingest the terminal evidence.

    Compute-then-approve mirrors SUBMIT: the trigger's own binding hash is derived and a
    settled G7 approval bound to *that* hash is required before the pipeline is touched, so
    the executed trigger cannot drift from what the operator approved (G7 authorizes both
    the submit and the trigger; G8 is only ever the failed-stage re-run). A terminal
    SUCCESS/FAILURE is joined with the pinned submission's binding half and ingested, which
    lands the run at RELEASE (success) or routes it to DIAGNOSE (failure). A non-terminal
    monitor result — a manual gate, a timeout, a transient transport fault — parks for a
    human rather than fabricating evidence.
    """
    binding = action.get("controller_binding") or {}
    module = binding.get("module")
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    from orchestrator import _ipipe_revision_set

    derived = _ipipe_revision_set(orchestrator, run_id, profile)
    if not derived.get("ok"):
        return derived
    revision_set = derived["revisions"]
    runtime = orchestrator.ipipe_runtime(run_id, ipipe_api)
    if isinstance(runtime, dict):  # the factory returns an error dict, never a falsy runtime
        return runtime
    # The trigger's G7 hash, derived without touching the pipeline, so the approval can be
    # requested for the exact binding that will be executed.
    hashed = runtime.trigger_input_hash(profile, revision_set, module)
    if not hashed.get("ok"):
        return hashed
    trigger_hash = hashed["input_hash"]
    approval_id = _settled_approval_id(orchestrator, run_id, "G7", trigger_hash)
    if approval_id is None:
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G7",
                "approval_input_hash": trigger_hash, "controller": "ipipe"}
    triggered = runtime.trigger(
        profile, revision_set, {"approval_id": approval_id, "input_hash": trigger_hash}, module
    )
    if not triggered.get("ok"):
        return {"ok": False, "reason_code": "IPIPE_TRIGGER_FAILED", "detail": triggered,
                "run_id": run_id}
    build_id = triggered["build_id"]
    deadline = (datetime.now(timezone.utc) + _IPIPE_MONITOR_WINDOW).isoformat()
    monitored = runtime.monitor(build_id, deadline)
    status = monitored.get("status")
    if status not in ("SUCCESS", "FAILURE"):
        # MANUAL_WAIT / TIMEOUT / TRANSIENT / INVALID: not the worker's call to make.
        return {"ok": True, "reason_code": "PARKED", "run_id": run_id, "parked": "IPIPE_MONITOR",
                "controller": "ipipe", "build_id": build_id, "monitor_status": status,
                "monitor": monitored}
    content = _build_ipipe_evidence(binding, monitored)
    ingested = orchestrator.phase_protocol(knowledge_sync).ingest_ipipe_evidence(run_id, content)
    if not ingested.get("ok"):
        return {"ok": False, "reason_code": "IPIPE_EVIDENCE_INGEST_FAILED", "detail": ingested,
                "run_id": run_id, "build_id": build_id}
    landed = orchestrator.status(run_id).get("state")
    return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id, "state": landed,
            "build_id": build_id, "monitor_status": status, "result": ingested}


def _build_ipipe_evidence(binding: dict[str, Any], monitored: dict[str, Any]) -> dict[str, Any]:
    """Join the pinned submission's binding half with the monitored outcome half.

    The binding half (pipeline_id / module / release_rule / revisions / environment
    fingerprint) comes from the IPIPE action's controller_binding, so it is exactly what
    G7 approved and cannot be re-supplied by the pipeline; the outcome half comes from
    `evidence_outcome`, which owns only the iPipe-vocabulary -> schema mapping. Together
    they form the `ipipe-evidence` document the protocol validates and binds on ingestion.
    """
    from clients.ipipe_runtime import evidence_outcome

    outcome = evidence_outcome(monitored)
    return {
        "pipeline_id": binding.get("pipeline_id"),
        "build_id": outcome["build_id"],
        "module": binding.get("module"),
        "revisions": binding.get("source_revisions"),
        "environment_fingerprint": binding.get("environment_fingerprint"),
        "release_rule": binding.get("release_rule"),
        "status": outcome["status"],
        "classification": outcome["classification"],
        "failure_signature": outcome["failure_signature"],
        "stages": outcome["stages"],
        "jobs": outcome["jobs"],
        "release_evidence": outcome["release_evidence"],
        "remote_evidence_refs": outcome["remote_evidence_refs"],
    }


def _execute_release(
    orchestrator: Any, run_id: str, action: dict[str, Any], ipipe_api: Any,
    knowledge_sync: Any | None = None,
) -> dict[str, Any]:
    """Verify the release under G9 and record it, landing the run at RELEASE_SUCCESS.

    G9 is the release gate: the worker records a release only once G9 is APPROVE for this
    exact release action. verify_release is read-only — it confirms the platform actually
    published the pinned build — so a release the platform has not published yet is not the
    worker's to force: it parks (RELEASE_WAITING). The recorded evidence's binding half
    (pipeline / module / revisions / release rule / environment) is read from the IPIPE
    predecessor, so a release can only ever be recorded for the build the pipeline proved.
    """
    approval_id = _settled_approval_id(orchestrator, run_id, "G9", action.get("input_hash"))
    if approval_id is None:
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G9",
                "approval_input_hash": action.get("input_hash"), "controller": "release"}
    protocol = orchestrator.phase_protocol(knowledge_sync)
    ipipe_artifact = protocol.artifacts.latest_phase(run_id, "IPIPE", None)
    ipipe_content = ipipe_artifact.get("envelope", {}).get("content") if ipipe_artifact.get("valid") else None
    if not isinstance(ipipe_content, dict) or not isinstance(ipipe_content.get("build_id"), str):
        return {"ok": False, "reason_code": "PREDECESSOR_REQUIRED", "run_id": run_id}
    build_id = ipipe_content["build_id"]
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    from orchestrator import _ipipe_revision_set

    derived = _ipipe_revision_set(orchestrator, run_id, pinned["profile"])
    if not derived.get("ok"):
        return derived
    runtime = orchestrator.ipipe_runtime(run_id, ipipe_api)
    if isinstance(runtime, dict):  # the factory returns an error dict, never a falsy runtime
        return runtime
    verified = runtime.verify_release(build_id, derived["revisions"])
    if not verified.get("ok"):
        if verified.get("status") == "RELEASE_WAITING":
            # The platform has not published the release yet: wait for it, do not force it.
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id, "parked": "RELEASE_WAITING",
                    "controller": "release", "build_id": build_id, "detail": verified}
        return {"ok": False, "reason_code": "RELEASE_VERIFY_FAILED", "detail": verified,
                "run_id": run_id, "build_id": build_id}
    content = _build_release_evidence(ipipe_content, verified)
    ingested = protocol.ingest_release_evidence(
        run_id, content, {"approval_id": approval_id, "input_hash": action.get("input_hash")}
    )
    if not ingested.get("ok"):
        return {"ok": False, "reason_code": "RELEASE_EVIDENCE_INGEST_FAILED", "detail": ingested,
                "run_id": run_id, "build_id": build_id}
    landed = orchestrator.status(run_id).get("state")
    return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id, "state": landed,
            "build_id": build_id, "release_id": verified.get("release_id"), "result": ingested}


def _build_release_evidence(ipipe_content: dict[str, Any], verified: dict[str, Any]) -> dict[str, Any]:
    """Join the IPIPE evidence's binding half with the verified release's outcome half.

    Pipeline / module / revisions come from the successful IPIPE evidence (so they cannot
    drift from what the pipeline proved); build / release id / rule / environment / refs
    come from verify_release. Together they form the `release-evidence` document the
    protocol validates and binds on ingestion.
    """
    return {
        "pipeline_id": ipipe_content.get("pipeline_id"),
        "build_id": verified["build_id"],
        "release_id": verified["release_id"],
        "module": ipipe_content.get("module"),
        "revisions": ipipe_content.get("revisions"),
        "environment_fingerprint": verified["environment_fingerprint"],
        "release_rule": verified["release_rule"],
        "status": "SUCCESS",
        "release_evidence": verified["evidence_refs"],
        "remote_evidence_refs": verified["evidence_refs"],
    }


# States a deterministic auto-advance may complete on its own. Controller side effects
# (submit / iPipe / release) are deliberately NOT auto-run here: they touch external
# systems and each keeps its own gate, so the loop parks on them for an explicit step.
def advance(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
            max_steps: int = 32, icode_skill: str = "/Users/tom/.comate/skills/.system/icode",
            icode_runtime: Any | None = None, ipipe_api: Any | None = None,
            locks: Any | None = None, owner_token: str | None = None) -> dict[str, Any]:
    """Drive a run forward, optionally under a single-writer run lease.

    When `locks` (a LockManager) is supplied the worker takes a per-run lease first, so two
    workers can never drive the same run concurrently; a run already driven by another live
    worker returns WORKER_LEASE_HELD instead of racing it. The lease is released as soon as
    the drive returns (a crashed holder's stale lease is taken over by the next worker). No
    `locks` means no lease — the established single-process behavior — so existing callers
    are unaffected.
    """
    if locks is None:
        return _drive(orchestrator, run_id, knowledge_sync, max_steps, icode_skill,
                      icode_runtime, ipipe_api)
    token = owner_token or uuid.uuid4().hex
    key = f"{_WORKER_LEASE_PREFIX}{run_id}"
    lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
    if not lease.get("acquired"):
        return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id,
                "owner_pid": lease.get("owner_pid")}
    try:
        return _drive(orchestrator, run_id, knowledge_sync, max_steps, icode_skill,
                      icode_runtime, ipipe_api)
    finally:
        released = locks.release(key, token)
        if isinstance(released, dict) and not released.get("released"):
            _log.warning("worker lease %s not released by %s: %s", key, token,
                         released.get("status"))


def _drive(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
           max_steps: int = 32, icode_skill: str = "/Users/tom/.comate/skills/.system/icode",
           icode_runtime: Any | None = None, ipipe_api: Any | None = None) -> dict[str, Any]:
    """Drive a run forward through every step the worker can take on its own, then park.

    Loops: completes AUTO_COMPLETE frontiers, and executes the WORKSPACE, SUBMIT and (when
    an `ipipe_api` transport is injected) IPIPE and RELEASE controllers when their gate is
    already settled (the compute-then-approve executors park with the exact hash when it is
    not). It parks — returning the decision the caller must arrange next — on a model phase
    (PRODUCER_WAIT, enqueuing a ProducerJob), an unsettled controller gate, a controller
    with no injected runtime (IPIPE/RELEASE without `ipipe_api`), a non-terminal pipeline
    monitor or an unpublished release, a terminal state, or a block. `max_steps` bounds it.

    WORKSPACE (local), SUBMIT (G7-gated CR), IPIPE (G7-gated trigger + monitor + ingest)
    and RELEASE (G9-gated verify + ingest) are the controllers the worker runs. Every
    controller execution goes through the orchestrator's own gate enforcement.
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
            action = decision["action"]
            job_id = f"producer:{action['action_id']}"
            existing = orchestrator.state.producer_job(job_id)
            if (isinstance(existing, dict) and existing.get("status") == "FULFILLED"
                    and isinstance(existing.get("draft"), dict)):
                # Auto-consume the persisted draft (HIGH-001): after approval, the worker
                # completes the phase from the stored draft — no new producer turn. A stale
                # draft cannot slip through: job_id is keyed on action_id, and _produce
                # re-validates and re-binds the approval to the current envelope.
                result = _produce(orchestrator, run_id, job_id, action, existing["draft"], knowledge_sync)
                if result.get("ok"):
                    steps.append({"phase": action.get("phase"), "result": result, "source": "cached-draft"})
                    continue
                if result.get("reason_code") == "APPROVAL_REQUIRED":
                    return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                            "parked": APPROVAL_WAIT, "gate": result.get("gate"),
                            "approval_input_hash": result.get("approval_input_hash"),
                            "auto_completed": steps}
                return {"ok": False, "reason_code": "PRODUCER_COMPLETE_FAILED", "run_id": run_id,
                        "detail": result, "auto_completed": steps}
            job = _enqueue_producer_job(orchestrator, run_id, decision)
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": PRODUCER_WAIT, "producer_job": job, "decision": decision,
                    "auto_completed": steps}
        if kind in (CONTROLLER_STEP, APPROVAL_WAIT):
            controller = (decision.get("action") or {}).get("controller")
            worker_owned = controller in ("workspace", "submit") or (
                controller in ("ipipe", "release") and ipipe_api is not None
            )
            if worker_owned:
                result = execute_controller(
                    orchestrator, run_id, knowledge_sync=knowledge_sync,
                    icode_skill=icode_skill, icode_runtime=icode_runtime, ipipe_api=ipipe_api,
                )
                if result.get("ok") and result.get("reason_code") != "PARKED":
                    steps.append({"controller": controller, "result": result})
                    continue
                if result.get("reason_code") == "PARKED":
                    return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                            "parked": result.get("parked"), "controller": controller,
                            "detail": result, "auto_completed": steps}
                if result.get("reason_code") == "APPROVAL_REQUIRED":
                    return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                            "parked": APPROVAL_WAIT, "controller": controller,
                            "gate": result.get("gate"),
                            "approval_input_hash": result.get("approval_input_hash"),
                            "auto_completed": steps}
                return {"ok": False, "reason_code": "CONTROLLER_STEP_FAILED", "run_id": run_id,
                        "controller": controller, "detail": result, "auto_completed": steps}
            # intake / ipipe / release without an injected runtime: not run by the loop.
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": kind, "controller": controller, "decision": decision,
                    "auto_completed": steps}
        # TERMINAL / BLOCKED
        return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                "parked": kind, "decision": decision, "auto_completed": steps}
    return {"ok": False, "reason_code": "MAX_STEPS_EXCEEDED", "run_id": run_id,
            "auto_completed": steps}


def resume(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
           icode_skill: str = "/Users/tom/.comate/skills/.system/icode",
           icode_runtime: Any | None = None, ipipe_api: Any | None = None,
           locks: Any | None = None, owner_token: str | None = None) -> dict[str, Any]:
    """Consume a run's settled approval-resume handoff and drive it forward.

    Run-scoped by construction (it only looks at this run's handoffs), which is the fix
    for the Stop-hook's global scan that stalled when two runs were parked (review #3):
    the worker resumes a specific run rather than guessing from a session-less Stop event.
    Verifies the handoff's approval is still APPROVE and bound to its input_hash, completes
    the handoff (idempotent), then hands off to advance().
    """
    candidates = [
        handoff for handoff in orchestrator.state.incomplete_handoffs(run_id)
        if (handoff.get("payload") or {}).get("kind") == "APPROVAL_RESUME"
    ]
    if not candidates:
        return {"ok": False, "reason_code": "NO_RESUME_HANDOFF", "run_id": run_id}
    if len(candidates) > 1:
        return {"ok": False, "reason_code": "MULTIPLE_RESUME_HANDOFFS", "run_id": run_id}
    handoff = candidates[0]
    data = handoff.get("payload") or {}
    approval = orchestrator.approvals.get(data.get("approval_id"))
    if not (
        isinstance(approval, dict)
        and approval.get("run_id") == run_id
        and approval.get("effective_decision") == "APPROVE"
        and approval.get("input_hash") == data.get("input_hash")
    ):
        return {"ok": False, "reason_code": "RESUME_HANDOFF_STALE", "run_id": run_id}
    orchestrator.state.complete_handoff(handoff["handoff_id"])
    return advance(orchestrator, run_id, knowledge_sync=knowledge_sync,
                   icode_skill=icode_skill, icode_runtime=icode_runtime, ipipe_api=ipipe_api,
                   locks=locks, owner_token=owner_token)


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
