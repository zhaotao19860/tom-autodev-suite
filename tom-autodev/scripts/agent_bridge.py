"""Agent-facing adapter over the durable orchestrator and worker driver.

The bridge resolves user-facing card identifiers and assembles configured worker
adapters. Workflow decisions, validation, approval binding, and state transitions
remain owned by ``worker_driver`` and ``Orchestrator``.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import worker_driver
import run_brief
import workflow_spec
import collaboration as collaboration_module

_RUN_ID = re.compile(r"^[0-9a-fA-F]{32}$")


def _json_safe(value: Any) -> Any:
    """Return a detached JSON-safe copy of a worker/bridge result."""
    import json

    return json.loads(json.dumps(value, ensure_ascii=False))


def resolve_run_target(orchestrator: Any, target: str) -> dict[str, Any]:
    """Resolve an explicit run id or an iCafe card id without guessing.

    Card lookup uses only the durable INTAKE identity in each run's event log.
    Multiple matches are returned as candidates so the caller can ask the user to
    choose one.
    """
    if not isinstance(target, str) or not target.strip():
        return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
    target = target.strip()
    if _RUN_ID.fullmatch(target):
        events = orchestrator.state.events(target)
        if not events:
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
        intake = events[0].get("payload") or {}
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": target,
            "state": events[-1].get("state"),
            "project": intake.get("project"),
            "requirement_id": intake.get("requirement_id"),
        }

    matches: list[dict[str, Any]] = []
    for latest in orchestrator.state.latest_states():
        run_id = latest.get("run_id")
        events = orchestrator.state.events(run_id) if isinstance(run_id, str) else []
        if not events:
            continue
        intake = events[0].get("payload") or {}
        if (
            intake.get("requirement_id") == target
            and events[-1].get("state") not in workflow_spec.terminal_states()
        ):
            matches.append({
                "run_id": run_id,
                "state": events[-1].get("state"),
                "project": intake.get("project"),
                "updated_at": events[-1].get("created_at"),
            })
    if not matches:
        return {"ok": False, "reason_code": "RUN_NOT_FOUND", "target": target}
    if len(matches) > 1:
        return {
            "ok": False,
            "reason_code": "AMBIGUOUS_RUN",
            "target": target,
            "candidates": matches,
        }
    return {
        "ok": True,
        "reason_code": "OK",
        "target": target,
        **matches[0],
        "requirement_id": target,
    }


def resolve_status_target(orchestrator: Any, target: str) -> dict[str, Any]:
    """Resolve status by card, including a unique terminal run as history.

    Active runs keep precedence. If there is no active match, exactly one terminal
    run resolves so callers can inspect its final state. Multiple terminal matches
    remain ambiguous. Action commands should keep using ``resolve_run_target``.
    """
    resolved = resolve_run_target(orchestrator, target)
    if resolved.get("ok") or resolved.get("reason_code") != "RUN_NOT_FOUND":
        return resolved
    if not isinstance(target, str) or not target.strip():
        return resolved
    target = target.strip()
    if _RUN_ID.fullmatch(target):
        return resolved

    terminal_states = set(workflow_spec.terminal_states())
    matches: list[dict[str, Any]] = []
    for latest in orchestrator.state.latest_states():
        run_id = latest.get("run_id")
        events = orchestrator.state.events(run_id) if isinstance(run_id, str) else []
        if not events:
            continue
        intake = events[0].get("payload") or {}
        state = events[-1].get("state")
        if intake.get("requirement_id") == target and state in terminal_states:
            matches.append({
                "run_id": run_id,
                "state": state,
                "project": intake.get("project"),
                "updated_at": events[-1].get("created_at"),
            })
    if not matches:
        return resolved
    if len(matches) > 1:
        return {
            "ok": False,
            "reason_code": "AMBIGUOUS_RUN",
            "target": target,
            "candidates": matches,
        }
    return {
        "ok": True,
        "reason_code": "OK",
        "target": target,
        **matches[0],
        "requirement_id": target,
    }


def _binding_error(orchestrator: Any, run_id: str) -> dict[str, Any] | None:
    events = orchestrator.state.events(run_id)
    if not events or not isinstance(events[0].get("payload"), dict):
        return None
    return collaboration_module.collaboration_binding_error(events[0]["payload"], run_id)


def _revision_context(orchestrator: Any, action: Any) -> dict[str, Any] | None:
    """What a redo has to answer: the feedback or reason, and the version it replaces.

    The producer keeps what the review did not object to and changes what it did;
    without the previous draft it could only start from scratch.
    """
    reentry = action.get("reentry") if isinstance(action, dict) else None
    if not isinstance(reentry, dict):
        return None
    context = {
        "kind": reentry.get("kind"),
        "instruction": reentry.get("feedback") or reentry.get("reason"),
        "actor": reentry.get("actor"),
        "approval_id": reentry.get("approval_id"),
        "from_state": reentry.get("from_state"),
        "redo": reentry.get("redo"),
        "profile_changed_keys": reentry.get("profile_changed_keys"),
    }
    job_id = reentry.get("previous_job_id")
    job = orchestrator.state.producer_job(job_id) if isinstance(job_id, str) and job_id else None
    if isinstance(job, dict) and isinstance(job.get("draft"), dict):
        context["previous_job_id"] = job_id
        context["previous_draft"] = job["draft"]
    return {key: value for key, value in context.items() if value is not None}


def _consume_local_infoflow_replies(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    """Consume journalled replies before the worker classifies the next action.

    The background watcher is deliberately optional.  A fresh CLI process must still
    reconcile a reply already appended to ``infoflow-replies.jsonl``; otherwise the
    approval ledger remains PENDING until somebody happens to run the watcher.  This
    only reads and settles an already delivered, hash-bound Infoflow request.  It
    never creates an approval and never chooses a decision.
    """
    from approval_contract import STRICT_APPROVAL_TIMEOUT_SECONDS
    from orchestrator import _infoflow_approval_client

    client = _infoflow_approval_client(orchestrator)
    outcomes: list[dict[str, Any]] = []
    for approval in orchestrator.approvals.pending():
        if approval.get("run_id") != run_id:
            continue
        if not any(
            isinstance(receipt, dict) and receipt.get("channel") == "infoflow"
            for receipt in approval.get("delivery_receipts") or []
        ):
            continue
        result = orchestrator.wait_infoflow_approval(
            run_id,
            approval["approval_id"],
            approval["input_hash"],
            client,
            STRICT_APPROVAL_TIMEOUT_SECONDS,
        )
        if isinstance(result, dict):
            outcomes.append(result)
    return outcomes


def _ensure_approval_resume_handoff(
    orchestrator: Any, run_id: str, approval_ids: set[str]
) -> None:
    """Persist the continuation checkpoint for a newly landed approval.

    This mirrors the watcher's handoff contract without sending a notification.  The
    worker remains the only component that advances a phase; the handoff merely makes
    ``continue`` idempotently choose the resume path after a CLI-side settlement.
    """
    events = orchestrator.state.events(run_id)
    latest = events[-1] if events else None
    if not isinstance(latest, dict):
        return
    from approval_ledger import gate_of
    current_gate = None
    state = latest.get("state")
    if state in workflow_spec.STATES:
        current_gate = workflow_spec.STATES[state].output_gate
    existing = {
        (item.get("payload") or {}).get("approval_id")
        for item in orchestrator.state.incomplete_handoffs(run_id)
        if (item.get("payload") or {}).get("kind") == "APPROVAL_RESUME"
    }
    from approval_watch import _later
    for approval in orchestrator.approvals.for_run(run_id):
        if approval.get("effective_decision") != "APPROVE" or approval.get("approval_id") in existing:
            continue
        # Approvals settled by this call are fresh by definition. Any other approval of
        # the current gate must postdate the current state event; an approval from an
        # earlier task or PLAN round would otherwise leave a resume handoff nothing
        # completes, and every later `continue` fails with MULTIPLE_RESUME_HANDOFFS.
        if approval.get("approval_id") not in approval_ids and (
            current_gate is None
            or gate_of(approval.get("action")) != current_gate
            or not _later(approval.get("resolved_at"), latest.get("created_at"))
        ):
            continue
        handoff_id = f"approval-resume-{approval['approval_id']}"
        try:
            orchestrator.state.record_handoff(
                run_id,
                handoff_id,
                {
                    "kind": "APPROVAL_RESUME",
                    "run_id": run_id,
                    "event_id": latest.get("event_id"),
                    "approval_id": approval["approval_id"],
                    "input_hash": approval.get("input_hash"),
                    "action": approval.get("action"),
                },
            )
        except ValueError as error:
            # A watcher may already have created the same handoff with an older
            # source event.  The durable handoff id is the idempotency key; a
            # completed/conflicting historical row must not make continue fail.
            if str(error) != "HANDOFF_CONFLICT":
                raise


def _apply_approved_workflow_spec_migration(
    orchestrator: Any, run_id: str
) -> dict[str, Any] | None:
    """Apply only the exact, already-approved migration for this run.

    Drift intentionally blocks the worker before it can advance.  ``continue`` is
    the narrow recovery path for a migration that has already been approved, so
    it may consume the existing approval but must not create, approve, or
    re-open one.  Recomputing the current plan also binds the apply to the
    current run, old pin, new pin and exact input hash.
    """
    from workflow_spec_migration import ACTION, apply, plan

    prepared = plan(orchestrator, run_id)
    if not prepared.get("ok"):
        return None
    expected_hash = prepared.get("input_hash")
    if not isinstance(expected_hash, str):
        return None
    candidates = [
        approval
        for approval in orchestrator.approvals.for_run(run_id)
        if (
            isinstance(approval, dict)
            and approval.get("run_id") == run_id
            and approval.get("action") == ACTION
            and approval.get("input_hash") == expected_hash
            and approval.get("effective_decision") == "APPROVE"
            and approval.get("approval_id")
        )
    ]
    if not candidates:
        return None
    approval = max(candidates, key=lambda item: item.get("resolved_at") or "")
    return apply(orchestrator, run_id, approval["approval_id"])


class AgentBridge:
    """Thin, stateless assembly boundary for agent-facing run operations."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        locks: Any,
        ipipe_api_factory: Callable[[str], Any] | None = None,
        icode_skill: str | None = None,
    ):
        self.orchestrator = orchestrator
        self.locks = locks
        self.ipipe_api_factory = ipipe_api_factory
        self.icode_skill = icode_skill

    def _worker_options(self, run_id: str) -> dict[str, Any]:
        options: dict[str, Any] = {"locks": self.locks}
        if self.ipipe_api_factory is not None:
            options["ipipe_api"] = self.ipipe_api_factory(run_id)
        if self.icode_skill is not None:
            options["icode_skill"] = self.icode_skill
        return options

    def drive(self, run_id: str) -> dict[str, Any]:
        blocked = _binding_error(self.orchestrator, run_id)
        if blocked is not None:
            return blocked
        settled = _consume_local_infoflow_replies(self.orchestrator, run_id)
        _ensure_approval_resume_handoff(
            self.orchestrator,
            run_id,
            {
                item["approval_id"]
                for item in settled
                if item.get("effective_decision") == "APPROVE"
            },
        )
        result = worker_driver.advance(
            self.orchestrator, run_id, **self._worker_options(run_id)
        )
        return _json_safe(result)

    def continue_run(self, run_id: str) -> dict[str, Any]:
        blocked = _binding_error(self.orchestrator, run_id)
        if blocked is not None:
            return blocked
        settled = _consume_local_infoflow_replies(self.orchestrator, run_id)
        migration = _apply_approved_workflow_spec_migration(self.orchestrator, run_id)
        if isinstance(migration, dict) and not migration.get("ok"):
            return _json_safe(migration)
        _ensure_approval_resume_handoff(
            self.orchestrator,
            run_id,
            {
                item["approval_id"]
                for item in settled
                if item.get("effective_decision") == "APPROVE"
            },
        )
        pending_approval_handoffs = [
            handoff for handoff in self.orchestrator.state.incomplete_handoffs(run_id)
            if (handoff.get("payload") or {}).get("kind") == "APPROVAL_RESUME"
        ]
        options = self._worker_options(run_id)
        # “需修改” is a settled human instruction, not a terminal stop.  Consume it
        # before asking the worker for the next action so typing “继续” after a
        # Chinese approval reply re-enters the producer and delivers a fresh card.
        # The explicit approval id and current input hash keep this bounded to the
        # exact rejected draft; no old approval is reused for the new content.
        if not pending_approval_handoffs:
            from approval_ledger import gate_of
            import run_change

            events = self.orchestrator.state.events(run_id)
            current = events[-1] if events else {}
            state = current.get("state")
            if state in workflow_spec.REVISABLE_STATES:
                candidates = [
                    approval for approval in self.orchestrator.approvals.for_run(run_id)
                    if (
                        gate_of(approval.get("action")) == workflow_spec.STATES[state].output_gate
                        and approval.get("effective_decision") == "REVISE"
                        and approval.get("input_hash")
                    )
                ]
                if candidates:
                    approval = max(candidates, key=lambda item: item.get("resolved_at") or "")
                    responses = self.orchestrator.approvals.responses(approval["approval_id"])
                    actor = next(
                        (
                            response.get("responder")
                            for response in responses
                            if response.get("effective")
                            and response.get("decision") == "REVISE"
                            and response.get("responder")
                        ),
                        "comate",
                    )
                    revised = run_change.revise(
                        self.orchestrator,
                        run_id,
                        approval["approval_id"],
                        feedback=None,
                        actor=actor,
                    )
                    if revised.get("reason_code") not in {"OK", "ALREADY_REVISED"}:
                        return _json_safe(revised)
        if pending_approval_handoffs:
            result = worker_driver.resume(self.orchestrator, run_id, **options)
        else:
            result = worker_driver.advance(self.orchestrator, run_id, **options)
        return _json_safe(result)

    def submit_draft(
        self, run_id: str, job_id: str, draft_content: dict[str, Any]
    ) -> dict[str, Any]:
        """Submit producer content under state-level idempotency, without a run lease.

        The worker lease serializes ``advance`` calls. Producer submission instead uses
        the ProducerJob transaction and idempotent phase/effect keys; this is not a
        global single-writer guarantee across every run mutation.
        """
        result = worker_driver.submit_draft(
            self.orchestrator, run_id, job_id, draft_content, locks=self.locks
        )
        return _json_safe(result)

    def report_producer_block(
        self, run_id: str, job_id: str, report: dict[str, Any]
    ) -> dict[str, Any]:
        return _json_safe(
            worker_driver.report_producer_block(
                self.orchestrator, run_id, job_id, report, locks=self.locks
            )
        )

    def report_producer_conflict(
        self, run_id: str, job_id: str, report: dict[str, Any]
    ) -> dict[str, Any]:
        return _json_safe(
            worker_driver.report_producer_conflict(
                self.orchestrator, run_id, job_id, report, locks=self.locks
            )
        )

    def claim_producer(
        self, run_id: str, job_id: str, agent_id: str, lease_seconds: int = 900
    ) -> dict[str, Any]:
        """Register that a host turn has accepted a ProducerJob."""
        return _json_safe(
            self.orchestrator.state.claim_producer_job(
                run_id, job_id, agent_id, lease_seconds
            )
        )

    def heartbeat_producer(
        self, run_id: str, job_id: str, agent_id: str, lease_seconds: int = 900
    ) -> dict[str, Any]:
        """Renew a host turn's ProducerJob lease."""
        return _json_safe(
            self.orchestrator.state.heartbeat_producer_job(
                run_id, job_id, agent_id, lease_seconds
            )
        )

    def release_producer(
        self, run_id: str, job_id: str, agent_id: str, *, completed: bool = False
    ) -> dict[str, Any]:
        """Release a host turn's ProducerJob lease."""
        return _json_safe(
            self.orchestrator.state.release_producer_job_agent(
                run_id, job_id, agent_id, completed=completed
            )
        )

    def status(self, target: str) -> dict[str, Any]:
        resolved = resolve_status_target(self.orchestrator, target)
        if not resolved.get("ok"):
            return _json_safe(resolved)
        return _json_safe(run_brief.build(self.orchestrator, resolved["run_id"]))

    def context(self, target: str) -> dict[str, Any]:
        """Return the pinned, read-only context needed to fill the current job."""
        resolved = resolve_run_target(self.orchestrator, target)
        if not resolved.get("ok"):
            return _json_safe(resolved)
        run_id = resolved["run_id"]
        self.orchestrator.state.expire_stale_producer_jobs(run_id)
        decision = worker_driver.classify_next(self.orchestrator, run_id, read_only=True)
        action = decision.get("action") if isinstance(decision, dict) else None
        action_view = None
        if isinstance(action, dict):
            action_view = {
                key: action.get(key)
                for key in (
                    "action_id", "source_event_id", "state", "phase", "task_id", "child_skill",
                    "controller", "result_schema", "required_human_gate", "input_hash",
                    "parent_artifact_hash", "source_revisions", "input_artifacts", "reentry",
                )
                if action.get(key) is not None
            }
        jobs = []
        if isinstance(action, dict) and action.get("action_id") and action.get("child_skill"):
            job = self.orchestrator.state.producer_job(f"producer:{action['action_id']}")
            if isinstance(job, dict):
                payload = job.get("payload") or {}
                jobs.append({
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                    "phase": payload.get("phase"),
                    "skill": payload.get("skill"),
                    "result_schema": payload.get("result_schema"),
                    "task_id": payload.get("task_id"),
                    "mode": payload.get("mode"),
                    "draft_present": isinstance(job.get("draft"), dict),
                    "updated_at": job.get("updated_at"),
                })
        result = {
            "ok": True,
            "reason_code": "OK",
            "run_id": run_id,
            "state": resolved.get("state"),
            "decision_kind": decision.get("kind") if isinstance(decision, dict) else None,
            "decision_reason_code": decision.get("reason_code") if isinstance(decision, dict) else None,
            "action": action_view,
            "producer_jobs": jobs,
        }
        revision = _revision_context(self.orchestrator, action)
        if revision is not None:
            result["revision_context"] = revision
        return _json_safe(result)

    def stop(self, target: str) -> dict[str, Any]:
        resolved = resolve_run_target(self.orchestrator, target)
        if not resolved.get("ok"):
            return _json_safe(resolved)
        return _json_safe(self.orchestrator.stop(resolved["run_id"]))
