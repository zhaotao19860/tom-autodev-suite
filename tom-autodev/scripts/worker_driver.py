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
  - CONTROLLER_STEP: a controller action (intake/workspace/submit/ipipe/release) whose gate is
                     settled (or ungated); the worker runs the deterministic transition
                     (a would-be Temporal activity).
"""

from __future__ import annotations

from execution_guard import guard_execution

import copy
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import workflow_spec
from approval_ledger import gate_of
from phase_protocol import _action_key, _canonical_hash, _pinned_profile_hash
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
            and gate_of(record.get("action")) == gate
            and record.get("effective_decision") == "APPROVE"
            and record.get("input_hash") == input_hash
        ):
            return True
    return False


def classify_next(orchestrator: Any, run_id: str, *, read_only: bool = False) -> dict[str, Any]:
    """Read-only: the single worker decision for the run's current frontier."""
    action = orchestrator.next(run_id, read_only=True) if read_only else orchestrator.next(run_id)
    if (
        not action.get("ok")
        and action.get("reason_code") == "ACTION_CONFLICT"
        and (
            action.get("state") == "DIAGNOSE"
            or orchestrator.status(run_id).get("state") == "DIAGNOSE"
        )
    ):
        candidates = [
            item for item in orchestrator.state.pending_producer_jobs(run_id)
            if (item.get("payload") or {}).get("phase") == "DIAGNOSE"
        ]
        recovered = [
            _recover_legacy_diagnose_action(orchestrator, run_id, item.get("job_id"))
            for item in candidates
        ]
        recovered = [item for item in recovered if item is not None]
        if len(recovered) == 1:
            action = recovered[0]
    state = action.get("state")
    # The read-only oracle reports terminal runs as ``ok=False`` with
    # ``TERMINAL_STATE``. Terminal is a normal completed decision, not a
    # recoverable block, so classify it before handling other refusals.
    if state in workflow_spec.terminal_states():
        return {"kind": TERMINAL, "state": state, "action": action}
    if not action.get("ok"):
        return {"kind": BLOCKED, "reason_code": action.get("reason_code"), "action": action}

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
    approval_input_hash = (
        action["input_hash"]
        if action.get("phase") == "INTAKE"
        else _canonical_hash({
            "action_id": action["action_id"],
            "task_id": action.get("task_id"),
            "parent_artifact_hash": action.get("parent_artifact_hash"),
            "source_revisions": source_revisions,
            "content_hash": content_hash,
        })
    )
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


def _recover_legacy_diagnose_action(
    orchestrator: Any, run_id: str, job_id: str,
) -> dict[str, Any] | None:
    """Recover one already-issued DIAGNOSE action after predecessor-ref repair.

    No job or idempotency row is rewritten.  The old action is reusable only when the
    current frontier is the same DIAGNOSE transition and the sole action-dict drift is
    ``source_evidence_refs``.  The protocol's verifier additionally binds the pending
    job to the current producer-block artifact and its nested evidence.
    """
    if not isinstance(job_id, str):
        return None
    job = orchestrator.state.producer_job(job_id)
    if (
        not isinstance(job, dict)
        or job.get("run_id") != run_id
        or job.get("status") not in {"PENDING", "FULFILLED"}
        or (job.get("status") == "FULFILLED" and not isinstance(job.get("draft"), dict))
    ):
        return None
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    if payload.get("phase") != "DIAGNOSE":
        return None
    events = orchestrator.state.events(run_id)
    current = events[-1] if events else {}
    if current.get("state") != "DIAGNOSE" or payload.get("source_event_id") != current.get("event_id"):
        return None
    if payload.get("task_id") != (current.get("payload") or {}).get("task_id"):
        return None
    profile_hash = _pinned_profile_hash(events, orchestrator.phase_protocol()._repin(events))
    key = _action_key(
        run_id, current.get("event_id"), "DIAGNOSE", payload.get("task_id"), profile_hash,
    )
    historical = orchestrator.state.idempotency_result(key)
    if not isinstance(historical, dict):
        return None
    if (
        historical.get("run_id") != run_id
        or historical.get("phase") != "DIAGNOSE"
        or historical.get("source_event_id") != payload.get("source_event_id")
        or historical.get("task_id") != payload.get("task_id")
        or historical.get("action_id") != payload.get("action_id")
        or job_id != f"producer:{historical.get('action_id')}"
    ):
        return None
    repaired = orchestrator.phase_protocol().legacy_diagnose_action(run_id)
    if not repaired.get("ok"):
        return None
    if not orchestrator.phase_protocol()._legacy_diagnose_binding_valid(run_id, historical):
        return None
    differing = {
        key for key in set(historical) | set(repaired)
        if historical.get(key) != repaired.get(key)
    }
    if differing != {"source_evidence_refs"}:
        return None
    evidence = (current.get("payload") or {}).get("evidence")
    report_id = evidence.get("producer_block_report_id") if isinstance(evidence, dict) else None
    if not isinstance(report_id, str) or not report_id:
        return None
    expected_evidence = [f"artifact:{report_id}"]
    repaired_evidence = repaired.get("source_evidence_refs")
    if repaired_evidence != expected_evidence:
        return None
    # Keep the historical action identity and all of its idempotency-bound fields,
    # but use the refs computed by the read-only repaired action.  Returning the
    # historical row here would rebuild an envelope with [] and make the
    # producer-block evidence disappear at PhaseProtocol.  The verifier above
    # proves the current run/task/revisions/parent artifact binding; the
    # read-only action supplies the only permitted delta.
    return {
        **historical,
        "source_evidence_refs": copy.deepcopy(
            repaired.get("source_evidence_refs") or []
        ),
    }


def _settled_approval_id(orchestrator: Any, run_id: str, gate: str, input_hash: str) -> str | None:
    for record in orchestrator.approvals.for_run(run_id):
        if (
            isinstance(record, dict)
            and gate_of(record.get("action")) == gate
            and record.get("effective_decision") == "APPROVE"
            and record.get("input_hash") == input_hash
        ):
            return record.get("approval_id")
    return None


@guard_execution
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


def report_producer_block(
    orchestrator: Any, run_id: str, job_id: str, report: dict[str, Any],
    *, locks: Any | None = None,
) -> dict[str, Any]:
    """Record a truthful IMPLEMENT block without manufacturing a Change Set."""
    if locks is not None:
        token = uuid.uuid4().hex
        key = f"{_WORKER_LEASE_PREFIX}{run_id}"
        lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
        if not lease.get("acquired"):
            return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id,
                    "owner_pid": lease.get("owner_pid")}
        try:
            return report_producer_block(orchestrator, run_id, job_id, report)
        finally:
            released = locks.release(key, token)
            if isinstance(released, dict) and not released.get("released"):
                _log.warning("worker lease %s not released by producer block %s", key, token)
    decision = classify_next(orchestrator, run_id, read_only=True)
    if decision.get("kind") != PRODUCER_WAIT:
        return {"ok": False, "reason_code": "NOT_PRODUCER", "run_id": run_id}
    action = decision.get("action") or {}
    expected_job = f"producer:{action.get('action_id')}"
    if action.get("phase") != "IMPLEMENT":
        return {"ok": False, "reason_code": "PRODUCER_BLOCK_PHASE_INVALID", "run_id": run_id}
    if job_id != expected_job:
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB", "expected_job_id": expected_job}
    job = orchestrator.state.producer_job(job_id)
    if not isinstance(job, dict) or job.get("run_id") != run_id:
        return {"ok": False, "reason_code": "PRODUCER_JOB_NOT_FOUND", "run_id": run_id}
    if job.get("status") == "FULFILLED":
        return {"ok": False, "reason_code": "PRODUCER_CANDIDATE_ALREADY_FULFILLED", "run_id": run_id}
    if job.get("status") != "PENDING":
        return {"ok": False, "reason_code": "PRODUCER_JOB_NOT_PENDING", "run_id": run_id}
    issues = validate_named_schema(report, "producer-block")
    if issues:
        return {"ok": False, "reason_code": "PRODUCER_BLOCK_SCHEMA_INVALID",
                "issues": [issue.__dict__ for issue in issues]}
    events = orchestrator.state.events(run_id)
    current = events[-1] if events else {}
    payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
    plan = orchestrator.artifacts.phase_artifact(report["plan_artifact_id"])
    envelope = plan.get("envelope") if plan.get("valid") else None
    if (
        report.get("run_id") != run_id
        or report.get("job_id") != job_id
        or report.get("action_id") != action.get("action_id")
        or report.get("source_event_id") != action.get("source_event_id")
        or report.get("task_id") != action.get("task_id")
        or report.get("input_hash") != action.get("input_hash")
        or report.get("plan_artifact_id") != payload.get("plan_artifact_id")
        or not isinstance(envelope, dict)
        or envelope.get("phase") != "PLAN"
        or envelope.get("run_id") != run_id
        or envelope.get("task_id") != action.get("task_id")
        or envelope.get("content_hash") != report.get("plan_content_hash")
        or envelope.get("source_revisions") != report.get("source_revisions")
        or envelope.get("approval_id") != report.get("plan_approval_id")
        or envelope.get("approval_input_hash") != report.get("plan_approval_input_hash")
        or action.get("source_revisions") != report.get("source_revisions")
    ):
        return {"ok": False, "reason_code": "PRODUCER_BLOCK_BINDING_MISMATCH", "run_id": run_id}
    approval = orchestrator.approvals.get(report["plan_approval_id"])
    if (
        not isinstance(approval, dict)
        or gate_of(approval.get("action")) != "G4"
        or approval.get("run_id") != run_id
        or approval.get("input_hash") != report["plan_approval_input_hash"]
        or approval.get("effective_decision") != "APPROVE"
    ):
        return {"ok": False, "reason_code": "APPROVED_PLAN_REQUIRED", "run_id": run_id}
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    report_id = _canonical_hash(report)
    stored = orchestrator.artifacts.put(
        run_id, "producer-block-report", canonical,
        {"report_id": report_id, "job_id": job_id, "action_id": action["action_id"],
         "source_event_id": action["source_event_id"], "task_id": action["task_id"],
         "plan_artifact_id": report["plan_artifact_id"]},
    )
    saved = orchestrator.state.record_producer_block(
        run_id, report_id, job_id, report, stored["artifact_id"]
    )
    return {"ok": True, "reason_code": "PLAN_INCOMPLETE_REPORTED", "run_id": run_id,
            "report_id": report_id, "artifact_id": stored["artifact_id"],
            "status": saved["status"], "worker_transition": "PENDING"}


def report_producer_conflict(
    orchestrator: Any, run_id: str, job_id: str, report: dict[str, Any],
    *, locks: Any | None = None,
) -> dict[str, Any]:
    """Record a PLAN conflict without manufacturing a task plan."""
    if locks is not None:
        token = uuid.uuid4().hex
        key = f"{_WORKER_LEASE_PREFIX}{run_id}"
        lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
        if not lease.get("acquired"):
            return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id}
        try:
            return report_producer_conflict(orchestrator, run_id, job_id, report)
        finally:
            locks.release(key, token)
    decision = classify_next(orchestrator, run_id, read_only=True)
    action = decision.get("action") or {}
    if decision.get("kind") != PRODUCER_WAIT or action.get("phase") != "PLAN":
        return {"ok": False, "reason_code": "PRODUCER_CONFLICT_PHASE_INVALID", "run_id": run_id}
    if job_id != f"producer:{action.get('action_id')}":
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB", "run_id": run_id}
    job = orchestrator.state.producer_job(job_id)
    if not isinstance(job, dict) or job.get("status") != "PENDING":
        return {"ok": False, "reason_code": "PRODUCER_JOB_NOT_PENDING", "run_id": run_id}
    issues = validate_named_schema(report, "producer-conflict")
    if issues:
        return {"ok": False, "reason_code": "PRODUCER_CONFLICT_SCHEMA_INVALID",
                "issues": [issue.__dict__ for issue in issues]}
    if report.get("job_id") != job_id or any(report.get(key) != action.get(key) for key in (
        "run_id", "action_id", "source_event_id", "task_id", "input_hash", "source_revisions"
    )):
        return {"ok": False, "reason_code": "PRODUCER_CONFLICT_BINDING_MISMATCH", "run_id": run_id}
    predecessor = orchestrator.artifacts.phase_artifact(report["predecessor_artifact_id"])
    envelope = predecessor.get("envelope") if predecessor.get("valid") else {}
    if (
        not predecessor.get("valid")
        or envelope.get("content_hash") != report["predecessor_content_hash"]
        or envelope.get("phase") != "TASKS"
    ):
        return {"ok": False, "reason_code": "PRODUCER_CONFLICT_PREDECESSOR_MISMATCH", "run_id": run_id}
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    report_id = _canonical_hash(report)
    stored = orchestrator.artifacts.put(
        run_id, "producer-conflict", canonical,
        {"report_id": report_id, "job_id": job_id, "action_id": action["action_id"],
         "source_event_id": action["source_event_id"], "task_id": action["task_id"],
         "predecessor_artifact_id": report["predecessor_artifact_id"]},
    )
    saved = orchestrator.state.record_producer_block(run_id, report_id, job_id, report, stored["artifact_id"])
    return {"ok": True, "reason_code": "SPEC_CONFLICT_REPORTED", "run_id": run_id,
            "report_id": report_id, "artifact_id": stored["artifact_id"], "status": saved["status"]}


def _consume_producer_block(orchestrator: Any, run_id: str, decision: dict[str, Any]) -> dict[str, Any] | None:
    action = decision.get("action") or {}
    if action.get("phase") not in {"IMPLEMENT", "PLAN"}:
        return None
    job_id = f"producer:{action.get('action_id')}"
    blocks = [
        item for item in orchestrator.state.pending_producer_blocks(run_id)
        if item.get("job_id") == job_id
        and item.get("report", {}).get("action_id") == action.get("action_id")
        and item.get("report", {}).get("source_event_id") == action.get("source_event_id")
    ]
    if not blocks:
        return None
    if len(blocks) != 1:
        return {"ok": False, "reason_code": "MULTIPLE_PRODUCER_BLOCKS", "run_id": run_id}
    job = orchestrator.state.producer_job(job_id)
    if not isinstance(job, dict) or job.get("status") != "PENDING" or job.get("draft"):
        # A real Change Set submitted after the block supersedes it; routing to
        # DIAGNOSE here would discard that draft and orphan its G5 request.
        return None
    block = blocks[0]
    report = block["report"]
    is_conflict = action.get("phase") == "PLAN"
    result = orchestrator.advance(run_id, "DIAGNOSE", {
        "input_hash": report["input_hash"],
        "approved_input_hash": report["input_hash"],
        "artifacts": ["producer-conflict", "task-dag"] if is_conflict else ["producer-block-report", "task-plan"],
        "producer_block_report_id": block["artifact_id"],
        "producer_block_report_hash": _canonical_hash(report),
        "plan_artifact_id": report.get("plan_artifact_id") or report.get("predecessor_artifact_id"),
        "plan_content_hash": report.get("plan_content_hash") or report.get("predecessor_content_hash"),
        "task_id": report["task_id"],
        "source_revisions": dict(report["source_revisions"]),
        "reason_code": "SPEC_CONFLICT" if is_conflict else "PLAN_INCOMPLETE",
        "classification": report.get("classification"),
        "evidence": report["evidence"],
    })
    if result.get("state") == "DIAGNOSE" and result.get("reason_code") == "OK":
        orchestrator.state.consume_producer_block(run_id, block["report_id"])
    return result


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
            "workflow_spec_hash": (
                workflow_spec.pinned_spec_hash(orchestrator.state.events(run_id))
                or workflow_spec.canonical_hash()
            ),
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
    *, locks: Any | None = None, owner_token: str | None = None,
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
    # Producer submission mutates the same run ledger as advance(). Serialize it with
    # the run lease when the caller is using the durable worker boundary; direct legacy
    # callers may omit locks and retain the historical single-process behavior.
    if locks is not None:
        token = owner_token or uuid.uuid4().hex
        key = f"{_WORKER_LEASE_PREFIX}{run_id}"
        lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
        if not lease.get("acquired"):
            return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id,
                    "owner_pid": lease.get("owner_pid")}
        try:
            return submit_draft(
                orchestrator, run_id, job_id, draft, knowledge_sync=knowledge_sync,
                locks=None,
            )
        finally:
            released = locks.release(key, token)
            if isinstance(released, dict) and not released.get("released"):
                _log.warning("worker lease %s not released by producer %s", key, token)
    if any(item.get("job_id") == job_id for item in orchestrator.state.pending_producer_blocks(run_id)):
        return {"ok": False, "reason_code": "PRODUCER_BLOCK_PENDING", "run_id": run_id,
                "job_id": job_id}
    decision = classify_next(orchestrator, run_id)
    # Workflow drift is not recoverable here: it must go through
    # WORKFLOW_SPEC_MIGRATION before any job is claimed or a receipt is written.
    if decision["kind"] == BLOCKED and decision.get("reason_code") == "ACTION_CONFLICT":
        recovered = _recover_legacy_diagnose_action(orchestrator, run_id, job_id)
        if recovered is not None:
            decision = {"kind": PRODUCER_WAIT, "skill": recovered.get("child_skill"),
                        "action": recovered}
    if decision["kind"] != PRODUCER_WAIT:
        return {"ok": False, "reason_code": "NOT_PRODUCER", "decision_kind": decision["kind"]}
    action = decision["action"]
    expected_job = f"producer:{action['action_id']}"
    if job_id != expected_job:
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB", "expected_job_id": expected_job}
    previous = orchestrator.state.producer_job(job_id)
    if (
        isinstance(previous, dict)
        and previous.get("status") == "FULFILLED"
        and action.get("phase") == "GRILL"
        and isinstance(previous.get("draft"), dict)
        and previous["draft"].get("decision_result") == "UNRESOLVED"
    ):
        reopened = orchestrator.state.reopen_invalid_producer_job(
            run_id,
            job_id,
            previous,
            action,
            {
                "ok": False,
                "reason_code": "DECISION_REQUIRED",
                "content_invalid": False,
                "retry_allowed": True,
            },
        )
        if not reopened.get("ok"):
            return reopened
    # A draft quoting a log (a `Bearer ...` line, an irepo header) is redacted before it
    # is validated, hashed or stored, so it is not refused for a credential it only
    # cites. Only a job with no recorded draft is redacted: once a draft is recorded an
    # approval may already be bound to its content hash, and redacting a resubmission
    # would change that hash -- such a draft keeps the ordinary refusal. IMPLEMENT is
    # never rewritten: its content is a patch whose bytes and hashes are the candidate.
    draft = _redacted_fresh_draft(previous, action, draft)
    agent_id = f"submit:{os.getpid()}"
    try:
        try:
            orchestrator.state.claim_producer_job(run_id, job_id, agent_id)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "job_id": job_id}
        result = _produce(orchestrator, run_id, job_id, action, draft, knowledge_sync)
        return result
    finally:
        try:
            orchestrator.state.release_producer_job_agent(
                run_id, job_id, agent_id,
                completed=orchestrator.state.producer_job(job_id).get("status") == "FULFILLED",
            )
        except (ValueError, AttributeError):
            pass


def _redacted_fresh_draft(previous: Any, action: dict[str, Any], draft: Any) -> Any:
    """`draft` redacted when that cannot move a bound hash; otherwise `draft` unchanged.

    Redacted: a job with no recorded draft (nothing is bound to its hash yet), or a
    resubmission whose redacted form *is* the recorded draft (the same bytes the
    approval was bound to, so the replay is consumed instead of refused). A different
    resubmission of a recorded draft and IMPLEMENT content are returned unchanged.
    """
    from persistence_policy import redact_secrets

    if not isinstance(draft, dict) or action.get("phase") == "IMPLEMENT":
        return draft
    recorded = previous.get("draft") if isinstance(previous, dict) else None
    if recorded is None:
        return redact_secrets(draft)
    cleaned = redact_secrets(draft)
    return cleaned if cleaned == recorded else draft


def _produce(
    orchestrator: Any, run_id: str, job_id: str, action: dict[str, Any],
    draft: dict[str, Any], knowledge_sync: Any | None,
) -> dict[str, Any]:
    """Validate a DraftContent, persist it, and complete the phase (or park on its gate).

    Shared by submit_draft (caller supplies the draft) and the worker's auto-consume path
    (draft read back from the persisted ProducerJob). fulfill/receipt are idempotent, so
    completing from a persisted draft after approval re-runs this safely.
    """
    recovered = _recover_invalid_producer_draft(
        orchestrator, run_id, job_id, action, knowledge_sync)
    if recovered is not None and not recovered.get("ok"):
        return {**recovered, "job_id": job_id}
    precheck = _validate_before_fulfill(
        orchestrator, run_id, draft, knowledge_sync, action=action)
    if precheck is not None:
        return {**precheck, "job_id": job_id}
    try:
        orchestrator.state.fulfill_producer_job(job_id, draft)
    except ValueError as error:
        return {"ok": False, "reason_code": str(error), "job_id": job_id}
    events = orchestrator.state.events(run_id)
    if workflow_spec.phase_mode_for_run(events, action["phase"]) == "merged":
        return _submit_merged(orchestrator, run_id, job_id, action, draft, knowledge_sync)

    envelope = build_envelope(action, draft)
    schema_name = action.get("result_schema")
    _record_model_receipt(orchestrator, run_id, action, draft, [schema_name])
    if action.get("phase") == "GRILL" and draft.get("decision_result") == "UNRESOLVED":
        return {
            "ok": False,
            "reason_code": "DECISION_REQUIRED",
            "phase": "GRILL",
            "choice_questions": draft.get("choice_questions") or [],
            "unresolved_frontier": draft.get("unresolved_frontier") or [],
            "job_id": job_id,
        }
    gate = action.get("required_human_gate")
    if gate is not None:
        approval_id = _settled_approval_id(orchestrator, run_id, gate, envelope["approval_input_hash"])
        if approval_id is None:
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": gate,
                    "approval_input_hash": envelope["approval_input_hash"], "job_id": job_id}
        envelope["approval_id"] = approval_id
    return orchestrator.complete_phase(run_id, envelope, knowledge_sync=knowledge_sync)


def _validate_before_fulfill(
    orchestrator: Any, run_id: str, content: Any, knowledge_sync: Any | None,
    *, action: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Use completion's entire approval-free validator before freezing a draft."""
    protocol = orchestrator.phase_protocol(knowledge_sync)
    action = action if action is not None else orchestrator.next(run_id)
    if not action.get("ok"):
        return action
    merged = workflow_spec.phase_mode_for_run(orchestrator.state.events(run_id), action["phase"]) == "merged"
    if merged:
        if not (isinstance(content, dict) and isinstance(content.get("spec"), dict)
                and isinstance(content.get("dag"), dict)):
            return {"ok": False, "reason_code": "MERGED_DRAFT_INVALID",
                    "content_invalid": True, "retry_allowed": True}
        checked = protocol.validate_merged_draft(
            action, build_envelope(action, content["spec"]), content["dag"])
    elif not isinstance(content, dict):
        checked = {"ok": False, "reason_code": "SCHEMA_INVALID", "content_invalid": True}
    else:
        checked = protocol.validate_draft(action, build_envelope(action, content))
    if checked.get("ok"):
        return None
    return {
        **checked,
        "reason_code": "DRAFT_SCHEMA_INVALID" if checked.get("reason_code") == "SCHEMA_INVALID" else checked.get("reason_code"),
        "retry_allowed": checked.get("content_invalid") is True,
        "validator": "PhaseProtocol.validate_merged_draft" if merged else "PhaseProtocol.validate_draft",
    }


def _recover_invalid_producer_draft(
    orchestrator: Any, run_id: str, job_id: str, action: dict[str, Any],
    knowledge_sync: Any | None,
) -> dict[str, Any] | None:
    """Reopen only a stored draft that fails the same validation as new production.

    An approval wait or transport failure is not proof of invalid content. The
    original bytes, hashes, receipt and cache evidence survive any successful CAS.
    """
    existing = orchestrator.state.producer_job(job_id)
    if not isinstance(existing, dict) or existing.get("status") != "FULFILLED":
        return None
    rejected = _validate_before_fulfill(
        orchestrator, run_id, existing.get("draft"), knowledge_sync, action=action)
    if rejected is None:
        return None
    if rejected.get("content_invalid") is not True:
        return rejected
    # A written artifact means completion already crossed the publication boundary.
    # Preserve it for explicit reconciliation even if a later validator rejects it.
    for artifact in orchestrator.artifacts.phase_artifacts(run_id, action["phase"]):
        if (artifact.get("envelope") or {}).get("action_id") == action.get("action_id"):
            return {"ok": False, "reason_code": "RECOVERY_REQUIRED"}
    return orchestrator.state.reopen_invalid_producer_job(
        run_id, job_id, existing, action, rejected)


def _submit_merged(
    orchestrator: Any, run_id: str, job_id: str, action: dict[str, Any],
    draft: dict[str, Any], knowledge_sync: Any | None,
) -> dict[str, Any]:
    """Complete a merged design front (SPEC + TASKS) from one {spec, dag} DraftContent.

    SPEC is gated (its own gate binds the spec content); TASKS is ungated (the owner's
    express declaration covers it). Both artifacts are still written and validated against
    their own schemas, so everything downstream reads a normal spec and a normal task-dag.
    """
    spec_envelope = build_envelope(action, draft["spec"])
    _record_model_receipt(orchestrator, run_id, action, draft, ["spec", "task-dag"])

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
    precheck = _validate_before_fulfill(
        orchestrator, run_id, draft["dag"], knowledge_sync, action=tasks_action)
    if precheck is not None:
        return {"ok": False, "reason_code": "MERGED_TASKS_FAILED", "detail": precheck}
    orchestrator.state.fulfill_producer_job(f"producer:{tasks_action['action_id']}", draft["dag"])
    tasks_envelope = build_envelope(tasks_action, draft["dag"])
    tasks_result = orchestrator.complete_phase(run_id, tasks_envelope, knowledge_sync=knowledge_sync)
    if not tasks_result.get("ok"):
        return {"ok": False, "reason_code": "MERGED_TASKS_FAILED", "detail": tasks_result}
    return {"ok": True, "reason_code": "MERGED_COMPLETE", "run_id": run_id,
            "spec": spec_result, "tasks": tasks_result}


def _recover_merged_tasks(orchestrator: Any, run_id: str, action: dict[str, Any]) -> bool:
    """Rebuild a merged bundle's TASKS ProducerJob from the persisted dag after a crash
    between the SPEC commit and the TASKS enqueue (R-M1).

    In a merged SPEC+TASKS run the SPEC is committed first, then the TASKS job is checkpointed
    with the dag (MEDIUM-002). If the process dies in between, the TASKS frontier has no
    fulfilled job and advance() would re-invoke the model. The merged producer job (keyed on
    the SPEC action) still holds the whole {spec, dag} draft, so this deterministically
    reconstructs the TASKS job from that dag; the caller's HIGH-001 auto-consume then completes
    TASKS with no new producer turn. Returns True when it reconstructed the job."""
    if action.get("phase") != "TASKS":
        return False
    tasks_job_id = f"producer:{action['action_id']}"
    existing = orchestrator.state.producer_job(tasks_job_id)
    if isinstance(existing, dict) and existing.get("status") == "FULFILLED":
        return False
    spec_artifact = orchestrator.artifacts.latest_phase(run_id, "SPEC", None)
    if not spec_artifact.get("valid"):
        return False
    spec_action_id = (spec_artifact.get("envelope") or {}).get("action_id")
    if not isinstance(spec_action_id, str) or not spec_action_id:
        return False
    # Only recover the TASKS frontier this SPEC completion produced — not a deliberate
    # WORKSPACE->TASKS re-split, which is a legitimate replan that must get a fresh DAG, not
    # the old bundle (R4-M1). The current frontier's event must be the SPEC->TASKS transition
    # (previous_state SPEC, carrying this SPEC artifact) and the action's own source event.
    events = orchestrator.state.events(run_id)
    current = events[-1] if events else {}
    payload = current.get("payload") or {}
    if (
        current.get("event_id") != action.get("source_event_id")
        or payload.get("previous_state") != "SPEC"
        or payload.get("artifact_id") != spec_artifact.get("artifact_id")
    ):
        return False
    merged = orchestrator.state.producer_job(f"producer:{spec_action_id}")
    if not (isinstance(merged, dict) and merged.get("status") == "FULFILLED"):
        return False
    dag = (merged.get("draft") or {}).get("dag")
    if not isinstance(dag, dict):
        return False
    if _validate_before_fulfill(orchestrator, run_id, dag, None, action=action) is not None:
        # A legacy bundle may have committed SPEC before discovering its invalid
        # DAG. Keep that bundle as evidence, and request a new TASKS draft against
        # the already-approved SPEC instead of freezing the same invalid DAG again.
        return False
    _enqueue_producer_job(
        orchestrator, run_id, {"action": action, "skill": action.get("child_skill")})
    orchestrator.state.fulfill_producer_job(tasks_job_id, dag)
    return True


@guard_execution
def execute_controller(
    orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
    icode_skill: str | None = None,
    icode_runtime: Any | None = None,
    ipipe_api: Any | None = None,
    collaboration_client: Any | None = None,
) -> dict[str, Any]:
    """Execute the current controller transition, if it is one the worker owns.

    INTAKE (G0-bound collaboration creation + requirement-snapshot completion), WORKSPACE
    (local, reversible worktree binding + gated WORKSPACE->PLAN) and SUBMIT
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
    if controller == "intake":
        approval_id = _settled_approval_id(
            orchestrator, run_id, "G0", action.get("input_hash")
        )
        if approval_id is None:
            return {
                "ok": False,
                "reason_code": "APPROVAL_REQUIRED",
                "run_id": run_id,
                "gate": "G0",
                "approval_input_hash": action.get("input_hash"),
            }

        events = orchestrator.state.events(run_id)
        payload = events[0].get("payload") if events else None
        if not isinstance(payload, dict):
            return {"ok": False, "reason_code": "INTAKE_PREREQUISITES_INVALID", "run_id": run_id}
        snapshot = payload.get("requirement_snapshot")
        binding = payload.get("collaboration_binding")
        project = payload.get("project")
        requirement_id = payload.get("requirement_id")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(binding, dict)
            or not isinstance(project, str)
            or not isinstance(requirement_id, str)
        ):
            return {"ok": False, "reason_code": "INTAKE_PREREQUISITES_INVALID", "run_id": run_id}

        # G0 authorizes creating the exact collaboration session pinned in INTAKE. The
        # durable receipt is idempotent, so a retry after an unknown remote outcome
        # reconciles/reads the same intent instead of creating a second group.
        if collaboration_client is None:
            from clients.infoflow_group_client import InfoflowGroupClient

            collaboration_client = InfoflowGroupClient()
        profile_result = orchestrator._runtime_profile(run_id)
        if not profile_result.get("ok"):
            return profile_result
        profile = profile_result["profile"]
        role_members = (
            (profile.get("approval_channels") or {}).get("role_members")
            if isinstance(profile, dict)
            else None
        )
        if not isinstance(role_members, dict):
            return {"ok": False, "reason_code": "MEMBER_CONFIRMATION_REQUIRED", "run_id": run_id}
        collaboration = orchestrator.collaboration_session(collaboration_client)
        group = collaboration.create(
            run_id,
            project,
            {"id": requirement_id, "title": snapshot.get("title")},
            role_members,
            approval_id=approval_id,
            input_hash=action.get("input_hash"),
        )
        if not isinstance(group, dict) or not group.get("group_id"):
            return {
                "ok": False,
                "reason_code": group.get("reason_code", "COLLABORATION_CREATE_FAILED")
                if isinstance(group, dict)
                else "COLLABORATION_CREATE_FAILED",
                "run_id": run_id,
                "detail": group,
            }

        envelope = build_envelope(action, approval_id=approval_id)
        completed = orchestrator.complete_phase(
            run_id, envelope, knowledge_sync=knowledge_sync
        )
        if completed.get("ok"):
            return {
                **completed,
                "controller": "intake",
                "collaboration": group,
            }
        return completed
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
    if controller in ("ipipe", "release") and ipipe_api is not None and knowledge_sync is None:
        # IPIPE/RELEASE evidence is published to KU like every phase artifact. complete_phase
        # resolves the run's own sync when none is injected; these controllers must too, or
        # every ingest fails at publish time after the evidence was already gathered.
        resolved = orchestrator.knowledge_sync(run_id)
        if isinstance(resolved, dict):
            return resolved
        knowledge_sync = resolved
    if controller == "ipipe" and ipipe_api is not None:
        return _execute_ipipe(orchestrator, run_id, action, ipipe_api, knowledge_sync=knowledge_sync)
    if controller == "release" and ipipe_api is not None:
        return _execute_release(orchestrator, run_id, action, ipipe_api, knowledge_sync=knowledge_sync)
    return {"ok": False, "reason_code": "CONTROLLER_NEEDS_RUNTIME", "controller": controller}


def _select_business_repo(
    orchestrator: Any, run_id: str, task_id: Any, business_repos: list[Any]
) -> dict[str, Any] | None:
    """The business repository a DAG task targets (MEDIUM-005).

    A multi-repo run must select by the task's declared business_module so a second repo's
    task is not cut against the first repo's worktree; a single-repo run is unambiguous and
    tolerates an advisory/absent module. Returns None when a multi-repo run's task names no
    registered module (or a legacy DAG carries none), so the caller refuses instead of
    silently defaulting to business_repos[0]."""
    repos = [repo for repo in business_repos if isinstance(repo, dict) and repo.get("path")]
    if not repos:
        return None
    module = orchestrator.task_business_module(run_id, task_id)
    if module:
        matched = [repo for repo in repos if repo.get("module") == module]
        if matched:
            return matched[0]
        return repos[0] if len(repos) == 1 else None
    return repos[0] if len(repos) == 1 else None


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
    business_repo = _select_business_repo(
        orchestrator, run_id, task_id, profile.get("business_repos") or []
    )
    if business_repo is None:
        # A cross-repo run whose DAG task does not name a registered business module has no
        # unambiguous worktree to cut (MEDIUM-005) -- refuse rather than default to repo 0.
        return {"ok": False, "reason_code": "WORKSPACE_TASK_REPO_UNRESOLVED",
                "run_id": run_id, "task_id": task_id}
    selected = {"business": business_repo, "tests": profile.get("test_repo")}
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
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    # A run may require several modules' pipelines to pass before RELEASE (HIGH-003 part 2).
    # Drive them ONE module per step: pick the next required module without passing evidence,
    # bind it to its own submission, and trigger/monitor/ingest it. A partial success keeps
    # the run in IPIPE and the driver loop re-enters here for the next module; the last
    # module's ingestion performs the transition. Recomputed from archived evidence each
    # call, so a restart resumes from the first module still outstanding.
    module = _next_ipipe_module(orchestrator, run_id, action, profile, knowledge_sync)
    if module is None:
        # No required module outstanding but the run is still in IPIPE: a crash between the
        # last module's evidence write and the IPIPE->RELEASE state commit (R-H2 part B).
        # Replay that finalize idempotently instead of dead-ending on the stuck state.
        finalized = _finalize_ipipe(orchestrator, run_id, knowledge_sync)
        if finalized is not None:
            return finalized
        return {"ok": False, "reason_code": "IPIPE_NO_OUTSTANDING_MODULE", "run_id": run_id}
    binding = _ipipe_module_binding(orchestrator, run_id, action, module)
    if not binding:
        # A required module the run never submitted has no binding to build from — refuse
        # rather than trigger an unapproved or wrong-revision pipeline.
        return {"ok": False, "reason_code": "IPIPE_MODULE_SUBMISSION_MISSING",
                "run_id": run_id, "module": module}
    from orchestrator import _ipipe_revision_set

    derived = _ipipe_revision_set(orchestrator, run_id, profile)
    if not derived.get("ok"):
        return derived
    revision_set = derived["revisions"]
    runtime = orchestrator.ipipe_runtime(run_id, ipipe_api)
    if isinstance(runtime, dict):  # the factory returns an error dict, never a falsy runtime
        return runtime
    # The trigger's G7 hash, derived without touching the pipeline, so the approval can be
    # requested for the exact binding that will be executed. Each module has its own hash.
    hashed = runtime.trigger_input_hash(profile, revision_set, module)
    if not hashed.get("ok"):
        return hashed
    trigger_hash = hashed["input_hash"]
    approval_id = _settled_approval_id(orchestrator, run_id, "G7", trigger_hash)
    adopted = None
    if approval_id is None:
        # iCode starts the module's ChangePipeline when the CR lands, so the build for these
        # exact revisions usually exists already. Adopting it writes nothing to iPipe and
        # needs no trigger approval; only a missing build asks a person to start one.
        adopted = runtime.discover(profile, revision_set, module)
        if adopted.get("reason_code") == "BUILD_QUERY_REQUIRED":
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G7",
                    "approval_input_hash": trigger_hash, "controller": "ipipe", "module": module}
        if not adopted.get("ok"):
            return {"ok": False, "reason_code": "IPIPE_ADOPT_FAILED", "detail": adopted,
                    "run_id": run_id, "module": module}
        triggered = adopted
    else:
        triggered = runtime.trigger(
            profile, revision_set, {"approval_id": approval_id, "input_hash": trigger_hash}, module
        )
    if not triggered.get("ok"):
        return {"ok": False, "reason_code": "IPIPE_TRIGGER_FAILED", "detail": triggered,
                "run_id": run_id}
    build_id = triggered["build_id"]
    deadline = (datetime.now(timezone.utc) + _IPIPE_MONITOR_WINDOW).isoformat()
    # Observe one bounded poll only. A long-running build is represented as PARKED
    # rather than holding the run lease and the Agent session for hours; continue/watch
    # re-enters this controller and reuses the same build identity.
    monitored = runtime.monitor_once(build_id, deadline)
    status = monitored.get("status")
    if status == "MONITORING":
        orchestrator.state.save_ipipe_monitoring(
            run_id,
            build_id,
            {
                "module": module,
                "status": status,
                "deadline": monitored.get("deadline"),
                "next_poll_after_seconds": monitored.get("next_poll_after_seconds"),
                "stages": monitored.get("stages") or [],
                "evidence_refs": monitored.get("evidence_refs") or [],
            },
        )
    else:
        # A terminal/manual observation supersedes the running checkpoint. Keep the
        # latest facts available to status, but make the terminal state explicit.
        orchestrator.state.save_ipipe_monitoring(
            run_id,
            build_id,
            {
                "module": module,
                "status": status,
                "stages": monitored.get("stages") or [],
                "stage_build_id": monitored.get("stage_build_id"),
                "failure_signature": monitored.get("failure_signature"),
                "evidence_refs": monitored.get("evidence_refs") or [],
            },
        )
    if status == "MANUAL_WAIT":
        stages = [stage for stage in monitored.get("stages") or []
                  if stage.get("status") in {
                      "PENDING_FOR_USER", "WAITING_FOR_MANUAL", "MANUAL", "PAUSED",
                      "WAITING_INPUT", "WAITING_FOR_CONFIRM",
                  }]
        stage_id = monitored.get("stage_build_id")
        if not stage_id and stages:
            stage_id = stages[0].get("stage_build_id")
        if not stage_id:
            return {"ok": False, "reason_code": "STAGE_OWNERSHIP_UNVERIFIED", "run_id": run_id}
        # A manual stage's inputs can be another changed module's product (x86bgw's p0
        # regression takes get_bgwagent). Its build only becomes this run's -- and its
        # product findable -- once adopted, which the one-module-per-step loop has not
        # done yet. Adopting is read-only (no trigger, no approval).
        from pipeline_plan import frozen_plan
        events_reader = getattr(orchestrator.state, "events", None)
        plan = (frozen_plan(events_reader(run_id)) if callable(events_reader) else None) or {}
        # Every changed module whose product a stage may consume: the required pipelines
        # and the product-test package (get_bgw_test_case is QA ChangePipeline 504074's).
        for other in adoptable_product_modules(plan):
            if other != module and callable(getattr(runtime, "discover", None)):
                runtime.discover(profile, revision_set, other)
        return _prepare_manual_stage(orchestrator, runtime, run_id, str(stage_id), module)
    if status not in ("SUCCESS", "FAILURE"):
        # MANUAL_WAIT / TIMEOUT / TRANSIENT / INVALID: not the worker's call to make.
        return {"ok": True, "reason_code": "PARKED", "run_id": run_id, "parked": "IPIPE_MONITOR",
                "controller": "ipipe", "build_id": build_id, "monitor_status": status,
                "module": module, "monitor": monitored}
    content = _build_ipipe_evidence(binding, monitored)
    ingested = orchestrator.phase_protocol(knowledge_sync).ingest_ipipe_evidence(run_id, content)
    if not ingested.get("ok"):
        if ingested.get("reason_code") == "PIPELINE_EVIDENCE_INCOMPLETE":
            # This module's evidence is archived and valid; other required modules remain, so
            # the run stays in IPIPE. Report success for this module so the driver loop
            # advances to the next one (its own G7 is requested when it is reached).
            return {"ok": True, "reason_code": "MODULE_INGESTED", "run_id": run_id,
                    "module": module, "build_id": build_id, "state": "IPIPE",
                    "outstanding_modules": ingested.get("outstanding_modules"), "result": ingested}
        return {"ok": False, "reason_code": "IPIPE_EVIDENCE_INGEST_FAILED", "detail": ingested,
                "run_id": run_id, "build_id": build_id}
    landed = orchestrator.status(run_id).get("state")
    return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id, "state": landed,
            "build_id": build_id, "monitor_status": status, "module": module, "result": ingested}


def adoptable_product_modules(plan: dict[str, Any]) -> list[str]:
    """Changed modules of the frozen plan whose ChangePipeline build this run may adopt."""
    modules = list(plan.get("required_modules") or [])
    for name, entry in (plan.get("product_inputs") or {}).items():
        if isinstance(entry, dict) and entry.get("changed") is True and name not in modules:
            modules.append(name)
    return modules


def _prepare_manual_stage(
    orchestrator: Any, runtime: Any, run_id: str, stage_id: str, module: str,
) -> dict[str, Any]:
    from orchestrator import _stage_parameters, _prepare_ipipe_g8

    # This path writes G8 material, so it is the one that records dependency evidence.
    resolved = _stage_parameters(orchestrator, runtime, run_id, [], stage_id, record_evidence=True)
    if not resolved.get("ok"):
        if resolved.get("reason_code") == "BRANCH_COMPILE_APPROVAL_REQUIRED":
            input_hash = resolved["input_hash"]
            material = {"binding": resolved["binding"], "parameters": {},
                        "product_target": resolved["binding"], "operation": "ipipe.branch_compile"}
            orchestrator.state.save_idempotency_result(
                f"ipipe.g8-material:{run_id}:{input_hash}", material)
            approval_id = _settled_approval_id(orchestrator, run_id, "G8", input_hash)
            if approval_id is None:
                return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G8",
                        "approval_input_hash": input_hash, "controller": "ipipe",
                        "run_id": run_id, "stage_build_id": stage_id,
                        "module": resolved["binding"]["module"], "material": material}
            result = runtime.ensure_branch_compile(
                resolved["binding"], {"approval_id": approval_id, "input_hash": input_hash})
            return {"ok": bool(result.get("ok")), "reason_code": result["reason_code"],
                    "run_id": run_id, "stage_build_id": stage_id, "detail": result,
                    **({"parked": "IPIPE_MONITOR"} if result.get("ok") else {})}
        return {**resolved, "run_id": run_id, "stage_build_id": stage_id}
    parameters = resolved["parameters"]
    prepared = _prepare_ipipe_g8(orchestrator, runtime, run_id, stage_id, resolved)
    if prepared.get("reason_code") == "MANUAL_STAGE_NOT_READY":
        # An earlier stage is still running: nothing to approve yet, keep watching.
        return {"ok": True, "reason_code": "PARKED", "parked": "IPIPE_MONITOR",
                "run_id": run_id, "controller": "ipipe", "module": module,
                "stage_build_id": stage_id, "waiting_on": prepared.get("waiting_on")}
    if prepared.get("reason_code") != "APPROVAL_REQUIRED":
        return prepared
    input_hash = prepared["approval_input_hash"]
    approval_id = _settled_approval_id(orchestrator, run_id, "G8", input_hash)
    if approval_id is None:
        return prepared
    executed = runtime.rerun(
        stage_id, {"approval_id": approval_id, "input_hash": input_hash}, parameters=parameters
    )
    if not executed.get("ok"):
        return executed
    return {"ok": True, "reason_code": "PARKED", "parked": "IPIPE_MONITOR",
            "run_id": run_id, "controller": "ipipe", "module": module,
            "stage_build_id": stage_id, "result": executed}


def _finalize_ipipe(
    orchestrator: Any, run_id: str, knowledge_sync: Any | None
) -> dict[str, Any] | None:
    """Replayable IPIPE->RELEASE finalize for the crash-before-commit window (R-H2 part B).

    Every required module has a current-binding SUCCESS (the caller reached here because no
    module is outstanding) yet the run is still in IPIPE -- the last module's evidence was
    archived and the process died before the transition committed. Re-ingesting that archived
    evidence is idempotent (same module action id re-stores to the same row) and, finding
    nothing outstanding, performs the IPIPE->RELEASE transition. Returns None when there is no
    replayable success evidence to finalize from (so the caller falls back to its stuck-state
    report rather than inventing a transition)."""
    from pipeline_plan import frozen_plan
    if frozen_plan(orchestrator.state.events(run_id)) is not None:
        result = orchestrator.phase_protocol(knowledge_sync).finalize_planned_ipipe(run_id)
        return {**result, "state": orchestrator.status(run_id).get("state"), "finalized": True}
    latest = orchestrator.artifacts.latest_phase(run_id, "IPIPE", None)
    if not latest.get("valid"):
        return None
    content = (latest.get("envelope") or {}).get("content")
    if not isinstance(content, dict) or content.get("status") != "SUCCESS":
        return None
    ingested = orchestrator.phase_protocol(knowledge_sync).ingest_ipipe_evidence(run_id, content)
    if not ingested.get("ok"):
        return None
    landed = orchestrator.status(run_id).get("state")
    return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id,
            "state": landed, "result": ingested, "finalized": True}


def _next_ipipe_module(
    orchestrator: Any, run_id: str, action: dict[str, Any], profile: dict[str, Any],
    knowledge_sync: Any | None = None,
) -> str | None:
    """The next module whose pipeline the worker should drive (HIGH-003 part 2).

    A profile that registers no `required_for_release` pipelines is the single-module case:
    use the module the IPIPE binding was pinned to. Otherwise defer to the protocol's
    binding-aware outstanding computation (R-H2) — a success recorded against an old revision
    (superseded by a repair) no longer counts, so the worker never skips a module whose
    current code was not actually built. Returns None once every required module has a
    current-binding success."""
    from pipeline_plan import frozen_plan
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is not None:
        outstanding = orchestrator.phase_protocol(knowledge_sync).ipipe_outstanding_modules(run_id)
        return outstanding[0] if outstanding else None
    from phase_protocol import _required_modules

    required = _required_modules(profile.get("pipeline_profile"))
    if not required:
        return (action.get("controller_binding") or {}).get("module")
    outstanding = orchestrator.phase_protocol(knowledge_sync).ipipe_outstanding_modules(run_id)
    return outstanding[0] if outstanding else None


def _ipipe_module_binding(
    orchestrator: Any, run_id: str, action: dict[str, Any], module: str
) -> dict[str, Any] | None:
    """The controller binding to build one module's pipeline evidence from.

    The primary module's binding is on the IPIPE action; every other required module is
    bound to the submission recorded for it, whose `controller_binding` carries the
    pipeline_id / release_rule / revisions / environment fingerprint that iPipe evidence and
    the G7 trigger hash are derived from (HIGH-003 part 2)."""
    from pipeline_plan import frozen_plan
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is not None:
        return plan["modules"].get(module)
    primary = action.get("controller_binding") or {}
    if primary.get("module") == module:
        return primary
    for event in reversed(orchestrator.state.events(run_id)):
        submissions = (event.get("payload") or {}).get("submissions")
        if not submissions:
            continue
        for submission in submissions:
            binding = (submission.get("controller_binding") or {}) if isinstance(submission, dict) else {}
            if binding.get("module") == module:
                return binding
        break
    return None


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


def _verify_required_releases(
    orchestrator: Any, run_id: str, runtime: Any, profile: dict[str, Any],
) -> dict[str, Any] | None:
    """Gate RELEASE_SUCCESS on EVERY required-for-release module being published (R-H3),
    verifying each module against ITS OWN build's revision set (R4-H2, per-module independent
    release).

    A profile that registers several `required_for_release` pipelines must not release on one
    module's build while another was never published; and each module is verified on the
    revision set its build passed on (`verify_release_of_build`), so an unchanged module's valid
    build is not stuck when a sibling module changed the current whole-repo set. Returns None
    when there are no required modules or all are verified-published; otherwise a RELEASE_WAITING
    park (a module not yet published) or a failure (verify failed / a required module has no
    success build)."""
    from pipeline_plan import frozen_plan, release_builds
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is not None:
        selected = release_builds(orchestrator.artifacts, orchestrator.state, run_id, plan)
        proofs = {}
        for module, artifact in selected.items():
            build_id = artifact["envelope"]["content"]["build_id"]
            verified = runtime.verify_planned_release(build_id, plan["modules"][module])
            if not verified.get("ok"):
                if verified.get("status") == "RELEASE_WAITING":
                    return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                            "parked": "RELEASE_WAITING", "controller": "release",
                            "module": module, "build_id": build_id, "detail": verified}
                return {"ok": False, "reason_code": "RELEASE_VERIFY_FAILED", "run_id": run_id,
                        "module": module, "build_id": build_id, "detail": verified}
            proofs[module] = verified["proof"]
        primary = proofs[plan["required_modules"][-1]]
        combined = {key: list(dict.fromkeys(ref for proof in proofs.values() for ref in proof[key]))
                    for key in ("release_evidence", "remote_evidence_refs")}
        return {"ok": True, "reason_code": "RELEASES_VERIFIED", "content": {
            **primary, **combined, "pipeline_plan_hash": plan["plan_hash"], "module_releases": proofs}}
    from phase_protocol import _required_modules

    required = _required_modules(profile.get("pipeline_profile"))
    if not required:
        return None
    builds: dict[str, str] = {}
    for artifact in orchestrator.artifacts.phase_artifacts(run_id, "IPIPE"):
        if not artifact.get("valid"):
            continue
        content = (artifact.get("envelope") or {}).get("content") or {}
        if (content.get("status") == "SUCCESS" and content.get("module") in required
                and isinstance(content.get("build_id"), str)):
            builds[content["module"]] = content["build_id"]
    for module in required:
        build_id = builds.get(module)
        if not build_id:
            return {"ok": False, "reason_code": "RELEASE_EVIDENCE_INCOMPLETE",
                    "run_id": run_id, "module": module}
        verified = runtime.verify_release_of_build(build_id)
        if not verified.get("ok"):
            if verified.get("status") == "RELEASE_WAITING":
                return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                        "parked": "RELEASE_WAITING", "controller": "release",
                        "module": module, "build_id": build_id, "detail": verified}
            return {"ok": False, "reason_code": "RELEASE_VERIFY_FAILED", "run_id": run_id,
                    "module": module, "build_id": build_id, "detail": verified}
    return None


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
    from pipeline_plan import frozen_plan
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is not None:
        runtime = orchestrator.ipipe_runtime(run_id, ipipe_api)
        if isinstance(runtime, dict):
            return runtime
        verified = _verify_required_releases(orchestrator, run_id, runtime, {})
        if verified.get("parked") == "RELEASE_WAITING":
            advanced = _advance_branch_release(orchestrator, run_id, runtime, verified)
            if advanced is not None:
                return advanced
        if verified.get("reason_code") != "RELEASES_VERIFIED":
            return verified
        ingested = protocol.ingest_release_evidence(
            run_id, verified["content"], {"approval_id": approval_id, "input_hash": action["input_hash"]})
        if not ingested.get("ok"):
            return {"ok": False, "reason_code": "RELEASE_EVIDENCE_INGEST_FAILED", "detail": ingested,
                    "run_id": run_id}
        return {"ok": True, "reason_code": "CONTROLLER_ADVANCED", "run_id": run_id,
                "state": orchestrator.status(run_id).get("state"), "result": ingested,
                "build_id": verified["content"]["build_id"], "release_id": verified["content"]["release_id"]}
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
    gate = _verify_required_releases(orchestrator, run_id, runtime, pinned["profile"])
    if gate is not None:
        # A multi-module release only lands once EVERY required-for-release module's build is
        # published — otherwise the last module's success would release the whole run while
        # another module was never published (R-H3).
        return gate
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


def _advance_branch_release(
    orchestrator: Any, run_id: str, runtime: Any, waiting: dict[str, Any],
) -> dict[str, Any] | None:
    """Move the BranchPipeline that releases a merged CR through its manual stages.

    G9 approved releasing this build, but the release itself is the target branch's
    BranchPipeline: after the CR merges, the platform starts that pipeline and it stops at
    manual stages (regression, test request, release). Each manual stage goes through the
    same path a ChangePipeline manual stage does -- parameters discovered from the live
    pipeline declaration, a hash-bound G8, the guarded rerun -- so nothing is typed by
    hand and nothing runs without approval. Returns None when there is nothing to drive.
    """
    detail = waiting.get("detail") or {}
    build_id = waiting.get("build_id")
    if not detail.get("branch_build_id") or not isinstance(build_id, str):
        return None
    bound = runtime.bind_branch_release_build(build_id)
    if not bound.get("ok"):
        return None
    branch_build = bound["branch_build_id"]
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    observed = runtime.monitor_once(branch_build, deadline)
    if observed.get("status") == "MANUAL_WAIT" and observed.get("stage_build_id"):
        # iPipe shows a downstream manual stage (发布) as PENDING_FOR_USER while earlier
        # stages are still running. Only the first stage that is not done may be driven:
        # asking for a G8 on the release while regression runs approves the wrong step.
        ready = _next_ready_manual_stage(observed.get("stages") or [])
        if ready is None:
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": "RELEASE_WAITING", "controller": "release",
                    "branch_build_id": branch_build, "monitor_status": "RUNNING"}
        prepared = _prepare_manual_stage(
            orchestrator, runtime, run_id, ready, waiting.get("module") or "",
        )
        return {**prepared, "controller": "release", "branch_build_id": branch_build}
    if observed.get("status") == "FAILURE":
        return {"ok": False, "reason_code": "BRANCH_RELEASE_FAILED", "run_id": run_id,
                "branch_build_id": branch_build, "monitor": observed}
    return {"ok": True, "reason_code": "PARKED", "run_id": run_id, "parked": "RELEASE_WAITING",
            "controller": "release", "branch_build_id": branch_build,
            "monitor_status": observed.get("status")}


_MANUAL_STAGE_STATUSES = frozenset({
    "PENDING_FOR_USER", "WAITING_FOR_MANUAL", "MANUAL", "PAUSED", "WAITING_INPUT",
    "WAITING_FOR_CONFIRM",
})
_DONE_STAGE_STATUSES = frozenset({"SUCC", "SUCCESS", "SUCCEEDED", "SKIPPED", "SKIP"})


def _next_ready_manual_stage(stages: list[dict[str, Any]]) -> str | None:
    """The first not-yet-done stage, when it is a manual one waiting for a person."""
    for stage in stages:
        status = str(stage.get("status") or "").upper()
        if status in _DONE_STAGE_STATUSES:
            continue
        if status in _MANUAL_STAGE_STATUSES and stage.get("stage_build_id"):
            return str(stage["stage_build_id"])
        return None
    return None


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
@guard_execution
def advance(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
            max_steps: int = 32, icode_skill: str | None = None,
            icode_runtime: Any | None = None, ipipe_api: Any | None = None,
            collaboration_client: Any | None = None, locks: Any | None = None,
            owner_token: str | None = None) -> dict[str, Any]:
    """Drive a run forward, optionally under a single-writer run lease.

    When `locks` (a LockManager) is supplied the worker takes a per-run lease first, so two
    workers can never drive the same run concurrently; a run already driven by another live
    worker returns WORKER_LEASE_HELD instead of racing it. The lease is released as soon as
    the drive returns (a crashed holder's stale lease is taken over by the next worker). No
    `locks` means no lease — the established single-process behavior — so existing callers
    are unaffected.
    """
    if locks is None and any(
        str(item.get("operation", "")).startswith("ku.")
        for item in orchestrator.state.pending_intents(run_id)
    ):
        locks = orchestrator.recovery.locks
    if locks is None:
        return _drive(orchestrator, run_id, knowledge_sync, max_steps, icode_skill,
                      icode_runtime, ipipe_api, collaboration_client)
    token = owner_token or uuid.uuid4().hex
    key = f"{_WORKER_LEASE_PREFIX}{run_id}"
    lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
    if not lease.get("acquired"):
        return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id,
                "owner_pid": lease.get("owner_pid")}
    try:
        return _drive(orchestrator, run_id, knowledge_sync, max_steps, icode_skill,
                      icode_runtime, ipipe_api, collaboration_client)
    finally:
        released = locks.release(key, token)
        if isinstance(released, dict) and not released.get("released"):
            _log.warning("worker lease %s not released by %s: %s", key, token,
                         released.get("status"))


def _drive(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
           max_steps: int = 32, icode_skill: str | None = None,
           icode_runtime: Any | None = None, ipipe_api: Any | None = None,
           collaboration_client: Any | None = None) -> dict[str, Any]:
    """Drive a run forward through every step the worker can take on its own, then park.

    Loops: completes AUTO_COMPLETE frontiers, and executes the INTAKE, WORKSPACE, SUBMIT and (when
    an `ipipe_api` transport is injected) IPIPE and RELEASE controllers when their gate is
    already settled (the compute-then-approve executors park with the exact hash when it is
    not). It parks — returning the decision the caller must arrange next — on a model phase
    (PRODUCER_WAIT, enqueuing a ProducerJob), an unsettled controller gate, a controller
    with no injected runtime (IPIPE/RELEASE without `ipipe_api`), a non-terminal pipeline
    monitor or an unpublished release, a terminal state, or a block. `max_steps` bounds it.

    INTAKE (G0-gated collaboration + snapshot), WORKSPACE (local), SUBMIT (G7-gated CR),
    IPIPE (G7-gated trigger + monitor + ingest)
    and RELEASE (G9-gated verify + ingest) are the controllers the worker runs. Every
    controller execution goes through the orchestrator's own gate enforcement.
    """
    pending = orchestrator.state.pending_intents(run_id)
    if any(item.get("operation") == "infoflow.group.create" for item in pending):
        if collaboration_client is None:
            from clients.infoflow_group_client import InfoflowGroupClient

            collaboration_client = InfoflowGroupClient()
        try:
            recovered = orchestrator.collaboration_session(
                collaboration_client
            ).reconcile_pending(run_id)
        except Exception as error:  # noqa: BLE001 - recovery must remain parked and auditable
            return {
                "ok": False,
                "reason_code": "RECOVERY_REQUIRED",
                "run_id": run_id,
                "detail": {"reason_code": str(error) or error.__class__.__name__},
            }
        if isinstance(recovered, dict) and recovered.get("reason_code") not in {None, "OK"}:
            return {
                "ok": False,
                "reason_code": "RECOVERY_REQUIRED",
                "run_id": run_id,
                "detail": recovered,
            }
    steps: list[dict[str, Any]] = []
    for _ in range(max_steps):
        decision = classify_next(orchestrator, run_id)
        kind = decision["kind"]
        if kind == AUTO_COMPLETE:
            result = execute_auto(orchestrator, run_id, knowledge_sync=knowledge_sync)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "reason_code": result.get("reason_code", "AUTO_COMPLETE_FAILED"),
                    "run_id": run_id,
                    "detail": result,
                    "auto_completed": steps,
                }
            steps.append({"phase": decision["action"].get("phase"), "result": result})
            continue
        if kind == PRODUCER_WAIT:
            action = decision["action"]
            blocked = _consume_producer_block(orchestrator, run_id, decision)
            if blocked is not None:
                if blocked.get("reason_code") == "OK":
                    steps.append({"phase": "IMPLEMENT", "result": blocked, "source": "producer-block"})
                    continue
                return {"ok": False, "reason_code": "PRODUCER_BLOCK_TRANSITION_FAILED",
                        "run_id": run_id, "detail": blocked, "auto_completed": steps}
            job_id = f"producer:{action['action_id']}"
            recovered = _recover_invalid_producer_draft(
                orchestrator, run_id, job_id, action, knowledge_sync)
            if recovered is not None and not recovered.get("ok"):
                return {"ok": False, "reason_code": "PRODUCER_COMPLETE_FAILED",
                        "run_id": run_id, "detail": recovered, "auto_completed": steps}
            existing = orchestrator.state.producer_job(job_id)
            if not (isinstance(existing, dict) and existing.get("status") == "FULFILLED"):
                # R-M1: a merged SPEC+TASKS bundle whose SPEC committed but whose TASKS job
                # was never enqueued (crash in that window) — reconstruct the TASKS job from
                # the merged producer's persisted dag so the frontier completes below via the
                # HIGH-001 auto-consume path, without re-invoking the model.
                if _recover_merged_tasks(orchestrator, run_id, action):
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
                return {"ok": False, "reason_code": result.get("reason_code", "PRODUCER_COMPLETE_FAILED"), "run_id": run_id,
                        "detail": result, "auto_completed": steps}
            job = _enqueue_producer_job(orchestrator, run_id, decision)
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": PRODUCER_WAIT, "producer_job": job, "decision": decision,
                    "auto_completed": steps}
        if kind in (CONTROLLER_STEP, APPROVAL_WAIT):
            controller = (decision.get("action") or {}).get("controller")
            worker_owned = controller in ("intake", "workspace", "submit") or (
                controller in ("ipipe", "release") and ipipe_api is not None
            )
            if worker_owned:
                result = execute_controller(
                    orchestrator, run_id, knowledge_sync=knowledge_sync,
                    icode_skill=icode_skill, icode_runtime=icode_runtime, ipipe_api=ipipe_api,
                    collaboration_client=collaboration_client,
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
            # IPIPE / RELEASE without an injected runtime are not run by the loop.
            return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                    "parked": kind, "controller": controller, "decision": decision,
                    "auto_completed": steps}
        # TERMINAL / BLOCKED
        return {"ok": True, "reason_code": "PARKED", "run_id": run_id,
                "parked": kind, "decision": decision, "auto_completed": steps}
    return {"ok": False, "reason_code": "MAX_STEPS_EXCEEDED", "run_id": run_id,
            "auto_completed": steps}


@guard_execution
def resume(orchestrator: Any, run_id: str, knowledge_sync: Any | None = None,
           icode_skill: str | None = None,
           icode_runtime: Any | None = None, ipipe_api: Any | None = None,
           collaboration_client: Any | None = None, locks: Any | None = None,
           owner_token: str | None = None) -> dict[str, Any]:
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
    if len(candidates) > 1:
        # Expired handoffs (a later explicit command already consumed their approval)
        # must not make the one valid handoff unusable; only provably stale ones go.
        from approval_watch import reap_stale_resume_handoffs

        candidates = reap_stale_resume_handoffs(
            orchestrator, run_id, candidates, actor="worker_driver.resume"
        )
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
                   collaboration_client=collaboration_client,
                   locks=locks, owner_token=owner_token)


def _enqueue_producer_job(orchestrator: Any, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """Record a ProducerJob for a PRODUCER_WAIT frontier (idempotent on the action id)."""
    action = decision["action"]
    job_id = f"producer:{action['action_id']}"
    payload = {
        "phase": action.get("phase"),
        "skill": decision.get("skill"),
        "mode": workflow_spec.phase_mode_for_run(
            orchestrator.state.events(run_id), action.get("phase")
        ),
        "result_schema": action.get("result_schema"),
        "input_hash": action.get("input_hash"),
        "action_id": action.get("action_id"),
        "source_event_id": action.get("source_event_id"),
        "task_id": action.get("task_id"),
        "required_human_gate": action.get("required_human_gate"),
        **({"reentry": action["reentry"]} if isinstance(action.get("reentry"), dict) else {}),
    }
    return orchestrator.state.record_producer_job(run_id, job_id, payload)
