from __future__ import annotations

import agent_host
from execution_guard import execution_guard, guard_execution, migration_exempt

import hashlib
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
import re
from pathlib import Path
from typing import Any

from approval_contract import (
    STRICT_APPROVAL_TIMEOUT_SECONDS,
    gateway_result_matches_request,
    is_clean_initial_pending_result,
    parse_gateway_result,
)
from approval_ledger import ApprovalLedger, gate_of, MAX_APPROVAL_RETRIES, _RETRY_MARKER
from artifact_store import ArtifactStore
from clients.infoflow_approval_client import ApprovalDeliveryBlocked
from clients.infoflow_bot_client import InfoflowRequestError
from collaboration import (
    CollaborationSession,
    build_collaboration_binding,
    group_id_for_run,
    intake_input_hash,
)
from evidence_gate import EvidenceGate
from evidence_policy import requirement_for
from knowledge_sync import KnowledgeSync, project_ku_target
from phase_protocol import PhaseProtocol, plan_implement_binding
from project_registry import (
    acceptable_profile_paths,
    load_profile,
    profile_path,
    requirement_profile_path,
    resolve_active_profile_path,
)
from requirement_snapshot import validation_error as snapshot_validation_error
from recovery import Recovery
from state_store import StateStore
from transition_policy import ALLOWED_TRANSITIONS, TransitionPolicy
from workspace_manager import WorkspaceManager
import workflow_spec


_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

_log = logging.getLogger(__name__)

# Exceptions that prove the call never reached the network: a method that is not
# there, a signature that does not match, a module that will not import. They are
# defects in this process, so they say nothing at all about the remote side, and
# treating them as uncertainty parks the run over a write that provably never
# happened. Every other exception -- a socket dying mid-request, an HTTP error, a
# timeout -- leaves the outcome genuinely unknown and must still be reconciled.
_NOTHING_SENT_EXCEPTIONS = (AttributeError, TypeError, NameError, ImportError)


def _pinned_profile_hash(orchestrator: Any, run_id: str, events: list[dict[str, Any]]) -> Any:
    """The hash this run is pinned to, honouring an approved re-pin over INTAKE's."""
    from profile_repin import pinned_hash, record

    return pinned_hash(events, record(orchestrator, run_id))


class Orchestrator:
    def __init__(self, config_root: Path | str | None = None):
        self.config_root = Path(config_root or (Path.home() / ".tom-autodev")).expanduser()
        self.require_requirement_profiles = False
        self.state = StateStore(self.config_root / "state.sqlite")
        self.recovery = Recovery(self.config_root / "state.sqlite")
        self.approvals = ApprovalLedger(self.config_root / "approvals.sqlite")
        self.artifacts = ArtifactStore(self.config_root / "artifacts")
        self.workspaces = WorkspaceManager(self.config_root / "worktrees")
        self.evidence_gate = EvidenceGate()
        self.transition_policy = TransitionPolicy()
        self._run_summary_options: dict[str, Any] = {}
        self._knowledge_sync_transport_options: dict[str, Any] = {}

    def start(
        self,
        requirement_id: str,
        project: str,
        *,
        requirement_snapshot: dict[str, Any] | None = None,
        change_class: str | None = None,
        require_requirement_profile: bool | None = None,
        fresh_token: str | None = None,
    ) -> dict[str, Any]:
        if require_requirement_profile is None:
            require_requirement_profile = self.require_requirement_profiles
        configured_profile_path = (
            requirement_profile_path(project, requirement_id, self.config_root)
            if require_requirement_profile
            else resolve_active_profile_path(project, requirement_id, self.config_root)
        )
        profile_result = load_profile(configured_profile_path)
        if not profile_result.get("ready"):
            return profile_result
        profile_hash = hashlib.sha256(configured_profile_path.read_bytes()).hexdigest()
        if requirement_snapshot is None:
            return {
                "ready": False,
                "reason_code": "ICAFE_SNAPSHOT_REQUIRED",
                "project": project,
                "requirement_id": requirement_id,
            }
        snapshot_error = snapshot_validation_error(requirement_snapshot, requirement_id)
        if snapshot_error is not None:
            return {
                "ready": False,
                "reason_code": snapshot_error,
                "project": project,
                "requirement_id": requirement_id,
            }
        if require_requirement_profile:
            from profile_wizard import confirmation_hash, confirmed_inputs

            profile = profile_result["profile"]
            confirmation = profile.get("profile_confirmation")
            if not isinstance(confirmation, dict):
                return {
                    "ready": False,
                    "reason_code": "PROFILE_CONFIRMATION_REQUIRED",
                    "profile_path": str(configured_profile_path),
                }
            expected_hash = confirmation_hash(confirmed_inputs(profile))
            if confirmation.get("confirmed_inputs_hash") != expected_hash:
                return {
                    "ready": False,
                    "reason_code": "PROFILE_CONFIRMATION_MISMATCH",
                    "profile_path": str(configured_profile_path),
                }
        snapshot_identity = requirement_snapshot["content_hash"]
        idempotency_key = (
            f"start:{project}:{requirement_id}:{profile_hash}:{snapshot_identity}:"
            f"{fresh_token or 'default'}"
        )
        existing = self.state.idempotency_result(idempotency_key)
        if existing is not None:
            return existing

        run_id = uuid.uuid4().hex
        prepared = _collaboration_binding(
            run_id, requirement_id, project, profile_hash,
            profile_result["profile"], requirement_snapshot,
        )
        if prepared.get("reason_code") != "OK":
            return {
                "ready": False,
                "reason_code": prepared.get("reason_code", "MEMBER_CONFIRMATION_REQUIRED"),
                "project": project,
                "requirement_id": requirement_id,
                **({"unresolved": prepared["unresolved"]} if "unresolved" in prepared else {}),
            }
        payload = {
            "requirement_id": requirement_id,
            "project": project,
            "profile_path": str(configured_profile_path),
            "profile_hash": profile_hash,
            "requirement_snapshot": requirement_snapshot,
            "collaboration_binding": prepared["binding"],
        }
        # The change class is confirmed at G0, so it and the workflow_spec version the run
        # runs under are bound INTO the G0 input hash (intake_hash_version v2): changing the
        # class, or re-editing class routing / knowledge scope (both fold into
        # workflow_spec_hash), changes the hash and voids any earlier APPROVE. classify_change
        # suggests from the card; an explicit override (owner's call, confirmed at G0) wins.
        # workflow_modes pins this class's phase-mode map so a later edit to CHANGE_CLASSES
        # cannot re-route this run — next/complete read it back via workflow_spec.
        change_class = workflow_spec.classify_change(requirement_snapshot, change_class)
        payload["change_class"] = change_class
        payload["workflow_spec_hash"] = workflow_spec.canonical_hash()
        payload["workflow_modes"] = workflow_spec.run_workflow_modes(change_class)
        # MVP §5: KU documents and iCafe comments no longer gate a phase; the local
        # artifact and the approval ledger do. Pinned per run, so runs started before keep
        # completing under v1 (dual read until they are all terminal).
        payload["evidence_contract"] = workflow_spec.EVIDENCE_CONTRACT
        payload["intake_hash_version"] = "v3"
        payload["g0_input_hash"] = intake_input_hash(payload)
        event = self.state.transition(
            run_id,
            "INTAKE",
            payload,
        )
        result = {
            "run_id": run_id,
            "state": "INTAKE",
            "event_id": event["event_id"],
            "project": project,
            "requirement_id": requirement_id,
        }
        self.state.save_idempotency_result(idempotency_key, result)
        return result

    @guard_execution
    def adopt_code_only_repair(
        self,
        run_id: str,
        source_run_id: str,
        target_run_id: str,
        task_id: str,
        diagnosis_artifact_id: str,
        plan_artifact_id: str,
    ) -> dict[str, Any]:
        """Adopt one approved CODE_ONLY repair into a fresh run.

        This is the narrow bridge from a terminal run to a new audit run. It
        copies only the TASKS and PLAN envelopes, rebinds them to the target
        run, and enters IMPLEMENT. Old approvals are never reused.
        """
        if run_id != target_run_id:
            return {"ok": False, "reason_code": "RUN_ID_MISMATCH", "run_id": target_run_id}
        if not all(isinstance(value, str) and value for value in (
            source_run_id, target_run_id, task_id, diagnosis_artifact_id, plan_artifact_id,
        )):
            return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": target_run_id}
        source = self.status(source_run_id)
        target = self.status(target_run_id)
        if source.get("state") != "STOPPED":
            return {"ok": False, "reason_code": "SOURCE_RUN_NOT_STOPPED", "run_id": source_run_id}
        # (c) A target that already adopted may only replay the same adoption. Any other
        # (diagnosis, plan) pair would silently re-bind a run whose IMPLEMENT is under way.
        adoption_events = [
            event for event in (target.get("events") or [])
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("adoption_kind") == "CODE_ONLY_REPAIR"
        ]
        if adoption_events:
            prior = adoption_events[-1]["payload"]
            if (
                prior.get("source_run_id") != source_run_id
                or prior.get("task_id") != task_id
                or prior.get("source_diagnosis_artifact_id") != diagnosis_artifact_id
                or prior.get("source_plan_artifact_id") != plan_artifact_id
            ):
                return {"ok": False, "reason_code": "ADOPTION_CONFLICT", "run_id": target_run_id,
                        "adopted_diagnosis_artifact_id": prior.get("source_diagnosis_artifact_id"),
                        "adopted_plan_artifact_id": prior.get("source_plan_artifact_id")}
            replay = self.state.idempotency_result(
                _adoption_result_key(target_run_id, diagnosis_artifact_id, plan_artifact_id))
            if isinstance(replay, dict):
                return {**replay, "replayed": True, "event_id": adoption_events[-1].get("event_id")}
            # Adopted before results were recorded: answer from the event, write nothing.
            artifacts = prior.get("adoption_artifacts") if isinstance(prior.get("adoption_artifacts"), dict) else {}
            return {"ok": True, "reason_code": "CODE_ONLY_REPAIR_ADOPTED", "replayed": True,
                    "run_id": target_run_id, "source_run_id": source_run_id, "task_id": task_id,
                    "event_id": adoption_events[-1].get("event_id"),
                    "tasks_artifact_id": artifacts.get("tasks"),
                    "plan_artifact_id": artifacts.get("plan")}
        if target.get("state") != "INTAKE":
            return {"ok": False, "reason_code": "TARGET_RUN_NOT_INTAKE", "run_id": target_run_id}

        diagnosis = self.artifacts.phase_artifact(diagnosis_artifact_id)
        plan = self.artifacts.phase_artifact(plan_artifact_id)
        if not diagnosis.get("valid") or not plan.get("valid"):
            return {"ok": False, "reason_code": "SOURCE_ARTIFACT_INVALID", "run_id": target_run_id}
        diagnosis_envelope = diagnosis["envelope"]
        plan_envelope = plan["envelope"]
        diagnosis_content = diagnosis_envelope.get("content") or {}
        if (
            diagnosis_envelope.get("run_id") != source_run_id
            or diagnosis_envelope.get("phase") != "DIAGNOSE"
            or diagnosis_envelope.get("task_id") != task_id
            or diagnosis_content.get("route") != "REPAIR"
            or diagnosis_content.get("repair_scope") != "CODE_ONLY"
        ):
            return {"ok": False, "reason_code": "CODE_ONLY_ADOPTION_INVALID", "run_id": target_run_id}
        # (b) Only the task's latest diagnosis is the source run's current answer; an
        # older one was superseded by later evidence.
        latest_diagnosis = next((
            artifact for artifact in reversed(self.artifacts.phase_artifacts(source_run_id, "DIAGNOSE"))
            if artifact.get("valid") and (artifact.get("envelope") or {}).get("task_id") == task_id
        ), None)
        if latest_diagnosis is None or latest_diagnosis.get("artifact_id") != diagnosis_artifact_id:
            return {"ok": False, "reason_code": "ADOPTION_DIAGNOSIS_NOT_LATEST", "run_id": target_run_id,
                    "latest_diagnosis_artifact_id": (latest_diagnosis or {}).get("artifact_id")}
        if (
            plan_envelope.get("run_id") != source_run_id
            or plan_envelope.get("phase") != "PLAN"
            or plan_envelope.get("task_id") != task_id
        ):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": target_run_id}
        # (b) The Plan must be the one the source run accepted for this task, still
        # descending from its current TASKS DAG (the same rule a CODE_ONLY repair
        # inside one run uses to return to IMPLEMENT).
        source_protocol = self.phase_protocol()
        accepted_plan = source_protocol._accepted_plan(source_run_id, task_id)
        if accepted_plan is None or accepted_plan.get("artifact_id") != plan_artifact_id:
            return {"ok": False, "reason_code": "ADOPTION_PLAN_NOT_CURRENT", "run_id": target_run_id,
                    "accepted_plan_artifact_id": (accepted_plan or {}).get("artifact_id")}
        # (a) The repair budget follows the work, not the run id: the target inherits the
        # source's confirmed rounds, and a source whose budget is spent cannot be revived
        # by adopting it into a fresh run.
        budget = _adoption_budget(source_protocol, self.state, source_run_id, diagnosis)
        if not budget["ok"]:
            return {**budget, "run_id": target_run_id, "source_run_id": source_run_id}

        approved = [
            approval for approval in self.approvals.for_run(source_run_id)
            if approval.get("action") == "G6"
            and approval.get("effective_decision") == "APPROVE"
            and approval.get("input_hash") == diagnosis_envelope.get("approval_input_hash")
        ]
        if not approved:
            return {"ok": False, "reason_code": "G6_APPROVAL_REQUIRED", "run_id": target_run_id}

        source_events = self.state.events(source_run_id)
        target_events = self.state.events(target_run_id)
        if not source_events or not target_events:
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": target_run_id}
        source_intake = source_events[0].get("payload") or {}
        target_intake = target_events[0].get("payload") or {}
        if (
            source_intake.get("requirement_id") != target_intake.get("requirement_id")
            or source_intake.get("project") != target_intake.get("project")
            or source_intake.get("profile_hash") != target_intake.get("profile_hash")
        ):
            return {"ok": False, "reason_code": "ADOPTION_BINDING_MISMATCH", "run_id": target_run_id}

        tasks = self.artifacts.latest_phase(source_run_id, "TASKS", None)
        if not tasks.get("valid"):
            return {"ok": False, "reason_code": "TASK_DAG_REQUIRED", "run_id": target_run_id}
        node = next(
            (item for item in (tasks["envelope"].get("content") or {}).get("nodes", [])
             if isinstance(item, dict) and item.get("task_id") == task_id),
            None,
        )
        if node is None:
            return {"ok": False, "reason_code": "TASK_NOT_IN_DAG", "run_id": target_run_id}

        target_module = node.get("business_module")
        trusted_receipts = []
        for result in self.state.external_results(source_run_id):
            intent = result.get("intent") or {}
            response = result.get("receipt", {}).get("response") or {}
            payload = intent.get("payload") or {}
            if (
                intent.get("operation") == "icode.submit"
                and response.get("ok") is True
                and response.get("reason_code") == "OK"
                and (not target_module or response.get("module") == target_module)
                # (d) A receipt without the task it belongs to cannot be attributed to
                # this task; trusting it could carry another task's CR into the target.
                and payload.get("task_id") == task_id
                and isinstance(response.get("change_number"), str)
                and isinstance(response.get("change_id"), str)
            ):
                trusted_receipts.append(response)
        if len(trusted_receipts) != 1:
            return {
                "ok": False,
                "reason_code": (
                    "SUBMISSION_RECEIPT_REQUIRED"
                    if not trusted_receipts else "SUBMISSION_RECEIPT_AMBIGUOUS"
                ),
                "run_id": target_run_id,
            }
        submission_identity = {
            "module": trusted_receipts[0]["module"],
            "change_number": trusted_receipts[0]["change_number"],
            "change_id": trusted_receipts[0]["change_id"],
        }

        def rebind(envelope: dict[str, Any], phase: str, parent_hash: str | None) -> dict[str, Any]:
            copied = dict(envelope)
            copied["run_id"] = target_run_id
            copied["action_id"] = hashlib.sha256(
                f"adopt:{target_run_id}:{phase}:{envelope['content_hash']}".encode()
            ).hexdigest()
            copied["source_event_id"] = target_events[0]["event_id"]
            copied["parent_artifact_hash"] = parent_hash
            copied["approval_id"] = None
            copied["approval_input_hash"] = None
            return copied

        copied_tasks = rebind(tasks["envelope"], "TASKS", None)
        tasks_result = self.artifacts.put_envelope(copied_tasks)
        copied_plan = rebind(plan_envelope, "PLAN", copied_tasks["content_hash"])
        profile = self._runtime_profile(target_run_id)
        if not profile.get("ok"):
            return profile
        profile_repos = {
            "business": profile["profile"].get("business_repos", []),
            "tests": [profile["profile"].get("test_repo", {})],
        }
        task_module = node.get("business_module") if isinstance(node, dict) else None
        if isinstance(task_module, str):
            profile_repos["business"] = [
                item for item in profile_repos["business"]
                if isinstance(item, dict) and item.get("module") == task_module
            ]
        normalized_repositories = []
        for repository in copied_plan["content"].get("repositories", []):
            role = repository.get("role") if isinstance(repository, dict) else None
            candidates = profile_repos.get(role, [])
            if not isinstance(role, str) or len(candidates) != 1:
                return {"ok": False, "reason_code": "ADOPTION_REPOSITORY_AMBIGUOUS", "run_id": target_run_id}
            normalized = dict(repository)
            normalized["path"] = candidates[0]["path"]
            normalized_repositories.append(normalized)
        copied_plan["content"] = dict(copied_plan["content"])
        copied_plan["content"]["repositories"] = normalized_repositories
        copied_plan["content_hash"] = hashlib.sha256(
            json.dumps(
                copied_plan["content"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        copied_plan["action_id"] = hashlib.sha256(
            f"adopt:{target_run_id}:PLAN:{copied_plan['content_hash']}".encode()
        ).hexdigest()
        plan_result = self.artifacts.put_envelope(copied_plan)
        plan_binding = plan_implement_binding(plan_result)
        if plan_binding is None:
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": target_run_id}
        payload = {
            "previous_state": "INTAKE",
            "task_id": task_id,
            "source_run_id": source_run_id,
            "source_diagnosis_artifact_id": diagnosis_artifact_id,
            "source_plan_artifact_id": plan_artifact_id,
            "adoption_kind": "CODE_ONLY_REPAIR",
            "source_revisions": plan_binding["source_revisions"],
            "plan_artifact_id": plan_binding["plan_artifact_id"],
            "plan_content_hash": plan_binding["plan_content_hash"],
            "parent_artifact_hash": copied_plan["content_hash"],
            "adoption_artifacts": {
                "tasks": tasks_result["artifact_id"],
                "plan": plan_result["artifact_id"],
                "diagnosis": diagnosis_artifact_id,
            },
            "submission_identity": submission_identity,
            "inherited_repair_rounds": budget["rounds"],
            "g6_approval_id": approved[0]["approval_id"],
        }
        # (e) INTAKE -> IMPLEMENT is not an edge of the workflow spec, and adding one
        # would change the spec hash every pinned run is checked against. The entry is a
        # controlled transition instead (like IPIPE_REBUILD): its policy decision names
        # the adoption evidence that authorizes it, and it is a compare-and-set on the
        # target's INTAKE event, so a concurrent change to the target fails the adoption
        # rather than racing it.
        decision = adoption_transition_decision(payload, approved[0])
        if not decision.get("allowed"):
            return {"ok": False, "reason_code": decision["reason_code"], "run_id": target_run_id}
        payload["policy_decision"] = decision
        result = {
            "ok": True,
            "reason_code": "CODE_ONLY_REPAIR_ADOPTED",
            "run_id": target_run_id,
            "source_run_id": source_run_id,
            "task_id": task_id,
            "tasks_artifact_id": tasks_result["artifact_id"],
            "plan_artifact_id": plan_result["artifact_id"],
            "inherited_repair_rounds": budget["rounds"],
        }
        committed = self.state.commit_transition_result(
            target_run_id, target_events[-1]["event_id"], "IMPLEMENT", payload,
            _adoption_result_key(target_run_id, diagnosis_artifact_id, plan_artifact_id), result,
        )
        status = committed.get("status")
        if status not in {"COMMITTED", "REPLAY"}:
            return {"ok": False, "reason_code": "ADOPTION_CONFLICT", "run_id": target_run_id,
                    "detail": status}
        stored = dict(committed["result"])
        adopted = [
            event for event in self.state.events(target_run_id)
            if (event.get("payload") or {}).get("adoption_kind") == "CODE_ONLY_REPAIR"
        ]
        stored["event_id"] = adopted[-1]["event_id"] if adopted else None
        return stored

    def status(self, run_id: str) -> dict[str, Any]:
        events = self.state.events(run_id)
        if not events:
            return {"run_id": run_id, "state": "RUN_NOT_FOUND", "events": []}
        latest = events[-1]
        return {"run_id": run_id, "state": latest["state"], "events": events}

    def next(self, run_id: str, *, read_only: bool = False) -> dict[str, Any]:
        """Return the next Comate-owned phase/controller action without remote writes."""
        drift = self._workflow_spec_drift(run_id)
        if drift is not None:
            return drift
        was_submit = self.status(run_id).get("state") == "SUBMIT"
        recovered = None if read_only else self._recover_skipped_submit(run_id)
        if isinstance(recovered, dict) and not recovered.get("ok"):
            return recovered
        if was_submit:
            stale = self._stale_submit_evidence(run_id, read_only=read_only)
            if stale is not None:
                return stale
        if read_only:
            return self.phase_protocol().next(run_id, read_only=True)
        return self.phase_protocol().next(run_id)

    def _workflow_spec_drift(self, run_id: str) -> dict[str, Any] | None:
        """Block a run whose pinned workflow_spec version no longer matches the live spec (R-M2).

        MEDIUM-001 pins `workflow_spec_hash` (and the class's phase modes) at G0, but knowledge
        scope, gates and transitions are still read from the live module. If the deployed spec
        changed under a paused run, continuing would run it under an unapproved policy while its
        receipt still cites the old version. Rather than silently re-label or re-route, refuse
        with `WORKFLOW_SPEC_DRIFT` before any side effect so the run is explicitly migrated (or
        the spec restored). A legacy run with no pin (pre-v2 INTAKE) is not guarded. The same
        pure check gates the protocol's completion/ingestion entries (R4-M2)."""
        return execution_guard(self.state, run_id)

    def _stale_submit_evidence(self, run_id: str, *, read_only: bool = False) -> dict[str, Any] | None:
        """Refuse a SUBMIT action whose Review no longer covers its Change Set."""
        current = self.status(run_id)
        if current.get("state") != "SUBMIT":
            return None
        action = self.phase_protocol().next(run_id, read_only=read_only)
        if not action.get("ok") or action.get("phase") != "SUBMIT":
            return None
        review = action.get("input_artifacts") or []
        if not review:
            return None
        review_artifact = self.artifacts.phase_artifact(review[0].get("artifact_id"))
        if not review_artifact or not review_artifact.get("valid"):
            return {"ok": False, "reason_code": "REVIEW_REFRESH_REQUIRED", "run_id": run_id}
        review_envelope = review_artifact.get("envelope")
        if not isinstance(review_envelope, dict):
            return {"ok": False, "reason_code": "REVIEW_REFRESH_REQUIRED", "run_id": run_id}
        review_revisions = review_envelope.get("source_revisions") or {}
        task_id = action.get("task_id")
        changes = [
            artifact for artifact in self.artifacts.artifacts_for_run(run_id)
            if artifact.get("kind") == "change-set"
            and (artifact.get("metadata") or {}).get("task_id") == task_id
            and (artifact.get("metadata") or {}).get("verdict") == "PASS"
        ]
        if not changes:
            return None
        try:
            change = json.loads(changes[-1]["content"].decode("utf-8"))
        except (AttributeError, KeyError, UnicodeDecodeError, ValueError):
            return {"ok": False, "reason_code": "REVIEW_REFRESH_REQUIRED", "run_id": run_id}
        revisions = change.get("revision_set") or {}
        current_revisions = {
            "business": (revisions.get("business") or {}).get("revision"),
            "tests": (revisions.get("test") or {}).get("revision"),
        }
        if not all(isinstance(value, str) and value for value in current_revisions.values()):
            return None
        if review_revisions != current_revisions:
            return {
                "ok": False,
                "reason_code": "REVIEW_REFRESH_REQUIRED",
                "run_id": run_id,
                "task_id": task_id,
                "review_revisions": review_revisions,
                "change_set_revisions": current_revisions,
            }
        return None

    def _recover_skipped_submit(self, run_id: str) -> dict[str, Any] | None:
        """Move a WORKSPACE that skipped SUBMIT back onto the reviewed task.

        A PASS used to jump to the next DAG node's WORKSPACE. The reviewed
        Change Set then sat unsubmitted while next() asked for a sibling's G4.
        New completions go to SUBMIT; this corrects a checkpoint that already
        took the old edge. It is a ledger repair, not a legal WORKSPACE→SUBMIT
        advance.
        """
        current = self.status(run_id)
        if current.get("state") != "WORKSPACE":
            return None
        events = current.get("events") or []
        if not events:
            return None
        payload = events[-1].get("payload") if isinstance(events[-1].get("payload"), dict) else {}
        if payload.get("previous_state") != "REVIEW":
            return None
        task_id = _latest_unsubmitted_reviewed_task(self, run_id)
        if task_id is None:
            # Legacy skipped-submit checkpoints may predate IMPLEMENT predecessor
            # metadata; recovery must still repair the already-recorded REVIEW edge.
            reviews = self.phase_protocol()._passing_reviews(run_id)
            if reviews:
                task_id = max(reviews, key=reviews.get)
        if task_id is None:
            return None
        source_event_id = events[-1]["event_id"]
        result_key = f"recover-skipped-submit:{run_id}:{source_event_id}"
        payload = {
            "previous_state": "WORKSPACE",
            "task_id": task_id,
            "reason_code": "SKIPPED_SUBMIT_RECOVERED",
        }
        result = {
            "ok": True,
            "reason_code": "SKIPPED_SUBMIT_RECOVERED",
            "task_id": task_id,
        }
        committed = self.state.commit_transition_result(
            run_id, source_event_id, "SUBMIT", payload, result_key, result,
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return None
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "RECOVERY_CONFLICT", "run_id": run_id}
        return None

    @guard_execution
    def recover_rebuilt_change_set(
        self, run_id: str, task_id: str, plan_artifact_id: str
    ) -> dict[str, Any]:
        """Re-enter IMPLEMENT from PLAN with one verified, immutable Plan pin.

        This is a local ledger repair for a change set that must be rebuilt.  It
        neither opens a worktree nor calls a remote adapter: the existing approved
        Plan is selected by artifact id, then its identity is carried into IMPLEMENT
        so a later Plan for the same task cannot silently replace its predecessor.
        """
        current = self.status(run_id)
        if current.get("state") == "RUN_NOT_FOUND":
            return current
        if not isinstance(task_id, str) or not task_id or not isinstance(plan_artifact_id, str) or not plan_artifact_id:
            return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
        latest = current.get("events", [])[-1] if current.get("events") else {}
        replay_payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
        if (
            current.get("state") == "IMPLEMENT"
            and replay_payload.get("reason_code") == "REBUILT_CHANGE_SET_RECOVERED"
            and replay_payload.get("task_id") == task_id
            and replay_payload.get("plan_artifact_id") == plan_artifact_id
        ):
            return {
                "ok": True, "reason_code": "REBUILT_CHANGE_SET_RECOVERED", "run_id": run_id,
                "state": "IMPLEMENT", "event_id": latest.get("event_id"), "task_id": task_id,
                "plan_artifact_id": plan_artifact_id,
                "plan_content_hash": replay_payload.get("plan_content_hash"),
            }
        if current.get("state") != "PLAN":
            return {
                "ok": False, "reason_code": "RECOVERY_STATE_INVALID", "run_id": run_id,
                "state": current.get("state"),
            }
        plan = self.artifacts.phase_artifact(plan_artifact_id)
        envelope = plan.get("envelope") if plan.get("valid") else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("run_id") != run_id
            or envelope.get("phase") != "PLAN"
            or envelope.get("task_id") != task_id
        ):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        transition = self.transition_policy.validate("PLAN", "IMPLEMENT")
        if not transition.get("allowed"):
            return {"ok": False, "reason_code": transition.get("reason_code"), "run_id": run_id}
        source_event_id = current["events"][-1]["event_id"]
        plan_hash = envelope.get("content_hash")
        result_key = f"recover-rebuilt-change-set:{run_id}:{source_event_id}:{task_id}:{plan_artifact_id}"
        result = {
            "ok": True, "reason_code": "REBUILT_CHANGE_SET_RECOVERED", "run_id": run_id,
            "task_id": task_id, "plan_artifact_id": plan_artifact_id, "plan_content_hash": plan_hash,
        }
        payload = {
            "previous_state": "PLAN", "task_id": task_id,
            "plan_artifact_id": plan_artifact_id, "plan_content_hash": plan_hash,
            "reason_code": "REBUILT_CHANGE_SET_RECOVERED", "policy_decision": transition,
        }
        committed = self.state.commit_transition_result(
            run_id, source_event_id, "IMPLEMENT", payload, result_key, result
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "RECOVERY_CONFLICT", "run_id": run_id}
        return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}

    @guard_execution
    def recover_stale_rebuilt_plan(
        self,
        run_id: str,
        task_id: str,
        expected_state: str,
        source_plan_artifact_id: str,
        source_revisions: dict[str, str],
    ) -> dict[str, Any]:
        """Locally replace a stale task Plan after explicit rebuilt revisions.

        This recovery deliberately does not publish, submit, request approval, or invoke a
        runtime adapter.  It creates a new immutable PLAN envelope from a verified prior
        Plan, removes its approval binding, and atomically moves only the explicitly named
        stale IMPLEMENT/SUBMIT checkpoint back to PLAN.  The resulting PLAN state therefore
        requires a normal fresh G4 approval before IMPLEMENT can resume.
        """
        current = self.status(run_id)
        if current.get("state") == "RUN_NOT_FOUND":
            return current
        if not isinstance(task_id, str) or not task_id or not isinstance(source_plan_artifact_id, str):
            return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
        latest = current.get("events", [])[-1] if current.get("events") else {}
        replay_payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
        if (
            current.get("state") == "PLAN"
            and replay_payload.get("reason_code") == "STALE_REBUILT_PLAN_RECOVERED"
            and replay_payload.get("previous_state") == expected_state
            and replay_payload.get("task_id") == task_id
            and replay_payload.get("source_plan_artifact_id") == source_plan_artifact_id
            and replay_payload.get("source_revisions") == source_revisions
        ):
            return {
                "ok": True, "reason_code": "STALE_REBUILT_PLAN_RECOVERED", "run_id": run_id,
                "state": "PLAN", "event_id": latest.get("event_id"), "task_id": task_id,
                "source_plan_artifact_id": source_plan_artifact_id,
                "plan_artifact_id": replay_payload.get("plan_artifact_id"),
                "plan_content_hash": replay_payload.get("plan_content_hash"),
                "source_revisions": dict(source_revisions), "approval_required": "G4",
            }
        if expected_state not in {"IMPLEMENT", "REVIEW", "SUBMIT"} or current.get("state") != expected_state:
            return {"ok": False, "reason_code": "RECOVERY_STATE_MISMATCH", "run_id": run_id,
                    "state": current.get("state")}
        if not isinstance(task_id, str) or not task_id or not isinstance(source_plan_artifact_id, str):
            return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
        if not _valid_source_revisions(source_revisions):
            return {"ok": False, "reason_code": "SOURCE_REVISION_REQUIRED", "run_id": run_id}
        events = current.get("events", [])
        payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
        if payload.get("task_id") != task_id:
            return {"ok": False, "reason_code": "RECOVERY_TASK_MISMATCH", "run_id": run_id}
        active_plan_id = _stale_checkpoint_plan(events, task_id)
        if active_plan_id != source_plan_artifact_id:
            return {"ok": False, "reason_code": "RECOVERY_PLAN_MISMATCH", "run_id": run_id}
        source = self.artifacts.phase_artifact(source_plan_artifact_id)
        envelope = source.get("envelope") if source.get("valid") else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("run_id") != run_id
            or envelope.get("phase") != "PLAN"
            or envelope.get("task_id") != task_id
        ):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        content = deepcopy(envelope.get("content"))
        if not isinstance(content, dict):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        repositories = content.get("repositories")
        if not isinstance(repositories, list):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        roles = {item.get("role") for item in repositories if isinstance(item, dict)}
        if roles != {"business", "tests"} or len(repositories) != 2:
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        for repository in repositories:
            if not isinstance(repository, dict):
                return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
            repository["revision"] = source_revisions[repository["role"]]
        # This is intentionally a new plan candidate rather than a re-approved document.
        # A fresh PLAN action will calculate its own G4 hash and approval binding.
        content["g4_input_hash"] = _canonical_hash({
            "recovery": "rebuilt-plan", "source_plan_content_hash": envelope["content_hash"],
            "source_revisions": source_revisions,
        })
        content_hash = _canonical_hash(content)
        action_id = _canonical_hash({
            "recovery": "stale-rebuilt-plan", "run_id": run_id, "task_id": task_id,
            "source_event_id": latest["event_id"], "source_plan_artifact_id": source_plan_artifact_id,
            "source_revisions": source_revisions,
        })
        cloned = {
            **deepcopy(envelope), "action_id": action_id, "source_event_id": latest["event_id"],
            "input_hash": content["g4_input_hash"], "content_hash": content_hash,
            "source_revisions": dict(source_revisions), "parent_artifact_hash": envelope["content_hash"],
            "approval_id": None, "approval_input_hash": None, "content": content,
        }
        try:
            archived = self.artifacts.put_envelope(cloned)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "run_id": run_id}
        result_key = (
            f"recover-stale-rebuilt-plan:{run_id}:{latest['event_id']}:{task_id}:"
            f"{source_plan_artifact_id}:{source_revisions['business']}:{source_revisions['tests']}"
        )
        result = {
            "ok": True, "reason_code": "STALE_REBUILT_PLAN_RECOVERED", "run_id": run_id,
            "task_id": task_id, "source_plan_artifact_id": source_plan_artifact_id,
            "plan_artifact_id": archived["artifact_id"], "plan_content_hash": content_hash,
            "source_revisions": dict(source_revisions), "approval_required": "G4",
        }
        transition_payload = {
            "previous_state": expected_state, "task_id": task_id,
            "source_plan_artifact_id": source_plan_artifact_id,
            "plan_artifact_id": archived["artifact_id"], "plan_content_hash": content_hash,
            "source_revisions": dict(source_revisions),
            "reason_code": "STALE_REBUILT_PLAN_RECOVERED", "approval_required": "G4",
        }
        committed = self.state.commit_transition_result(
            run_id, latest["event_id"], "PLAN", transition_payload, result_key, result
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "RECOVERY_CONFLICT", "run_id": run_id}
        return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}

    @guard_execution
    def recover_stale_submit(
        self, run_id: str, task_id: str, plan_artifact_id: str
    ) -> dict[str, Any]:
        """Return a stale SUBMIT checkpoint to PLAN before rebuilding IMPLEMENT."""
        current = self.status(run_id)
        if current.get("state") != "SUBMIT":
            return {"ok": False, "reason_code": "RECOVERY_STATE_INVALID",
                    "run_id": run_id, "state": current.get("state")}
        plan = self.artifacts.phase_artifact(plan_artifact_id)
        envelope = plan.get("envelope") if plan.get("valid") else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("run_id") != run_id
            or envelope.get("phase") != "PLAN"
            or envelope.get("task_id") != task_id
        ):
            return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID", "run_id": run_id}
        source_event_id = current["events"][-1]["event_id"]
        payload = {
            "previous_state": "SUBMIT", "task_id": task_id,
            "plan_artifact_id": plan_artifact_id,
            "plan_content_hash": envelope.get("content_hash"),
            "source_revisions": dict(envelope.get("source_revisions") or {}),
            "reason_code": "REVIEW_REFRESH_REQUIRED",
        }
        result = {"ok": True, "reason_code": "STALE_SUBMIT_RECOVERED",
                  "run_id": run_id, "state": "PLAN", "task_id": task_id,
                  "plan_artifact_id": plan_artifact_id}
        key = f"recover-stale-submit:{run_id}:{source_event_id}:{task_id}:{plan_artifact_id}"
        committed = self.state.commit_transition_result(
            run_id, source_event_id, "PLAN", payload, key, result
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "RECOVERY_CONFLICT", "run_id": run_id}
        return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}

    def recover_legacy_pipeline_plan(self, run_id: str) -> dict[str, Any]:
        """Backfill a frozen pipeline plan for a run that entered IPIPE before plans existed.

        A run created under an older build has an IPIPE event with no ``pipeline_plan``,
        so RELEASE fails closed with ``PIPELINE_PLAN_REQUIRED``.  This recovery reconstructs
        the plan deterministically from the run's own immutable, reviewed submissions and its
        pinned profile — exactly what a fresh SUBMIT->IPIPE would freeze now — and appends it
        as an IPIPE event.  It does not submit, publish, approve, or call any runtime adapter,
        and it changes no external state.  Any in-flight build is re-driven per module: an old
        build cannot satisfy the frozen plan because its evidence never carries the plan's
        binding hash, so this only unblocks RELEASE, it never blesses a stale build.
        """
        current = self.status(run_id)
        if current.get("state") == "RUN_NOT_FOUND":
            return current
        events = current.get("events", [])
        ipipe_events = [event for event in events if event.get("state") == "IPIPE"]
        if not ipipe_events:
            return {"ok": False, "reason_code": "RECOVERY_STATE_MISMATCH",
                    "run_id": run_id, "state": current.get("state")}
        latest_payload = ipipe_events[-1].get("payload")
        if isinstance(latest_payload, dict) and latest_payload.get("pipeline_plan") is not None:
            # A fresh run already carries its plan, and a second migration call replays
            # to this same idempotent no-op rather than stacking another IPIPE event.
            return {"ok": True, "reason_code": "PIPELINE_PLAN_PRESENT",
                    "run_id": run_id, "state": current.get("state")}
        entry_payload = ipipe_events[0].get("payload") or {}
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        profile = pinned["profile"]
        from pipeline_plan import create_plan
        try:
            plan = create_plan(self, run_id, profile)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "run_id": run_id}
        repos = profile.get("business_repos") or []
        primary_module = repos[0].get("module") if repos and isinstance(repos[0], dict) else None
        primary_target = plan["modules"].get(primary_module) or plan["modules"][plan["required_modules"][0]]
        intake = events[0]["payload"]
        result_key = f"recover-legacy-pipeline-plan:{run_id}:{ipipe_events[0]['event_id']}"
        payload = _ipipe_entry_payload(
            intake, plan, primary_target,
            entry_payload.get("approval_id"), entry_payload.get("approval_input_hash"),
            previous_state="IPIPE", extra={"reason_code": "LEGACY_PIPELINE_PLAN_MIGRATED"},
        )
        result = {"ok": True, "reason_code": "LEGACY_PIPELINE_PLAN_MIGRATED",
                  "run_id": run_id, "state": "IPIPE",
                  "pipeline_plan_hash": plan["plan_hash"],
                  "submission_artifact_id": primary_target["artifact_id"]}
        committed = self.state.commit_transition_result(
            run_id, events[-1]["event_id"], "IPIPE", payload, result_key, result
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "RECOVERY_CONFLICT", "run_id": run_id}
        return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}

    @guard_execution
    def complete_phase(
        self,
        run_id: str,
        envelope: dict[str, Any],
        *,
        knowledge_sync: Any | None = None,
    ) -> dict[str, Any]:
        sync = knowledge_sync if knowledge_sync is not None else self.knowledge_sync(run_id)
        if isinstance(sync, dict):
            return sync
        result = self.phase_protocol(sync).complete(run_id, envelope)
        # A passed Review is the last moment the reviewed bytes are still identifiable, so
        # the submit descriptor is built here rather than at SUBMIT: it pins the commit the
        # review actually saw. Without it the iCode boundary rejects every submission with
        # CHANGE_SET_REVIEW_REQUIRED.
        if result.get("phase_complete") and result.get("phase") == "REVIEW":
            from submit_descriptor import build_and_archive

            descriptor = build_and_archive(self, run_id, result.get("task_id"))
            result = {**result, "submit_descriptor": descriptor}
        return result

    @guard_execution
    def optimize(
        self,
        run_id: str,
        operation: str,
        *,
        summary: dict[str, Any] | None = None,
        allowed_roots: list[Path] | None = None,
        proposal_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        if operation == "build":
            return self.run_summary().build(run_id)
        if operation == "propose":
            if not isinstance(summary, dict) or not isinstance(allowed_roots, list):
                return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
            if summary.get("run_id") != run_id:
                return {"ok": False, "reason_code": "OPTIMIZATION_RUN_MISMATCH", "run_id": run_id}
            knowledge_sync = self._owned_knowledge_sync(run_id)
            if isinstance(knowledge_sync, dict):
                return knowledge_sync
            return self.run_summary(knowledge_sync).propose(summary, allowed_roots)
        if operation == "apply":
            if not all(isinstance(value, str) and value for value in (proposal_id, approval_id)):
                return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
            proposal = self.state.optimization_proposal(proposal_id)
            approval = self.approvals.get(approval_id)
            if (
                not isinstance(proposal, dict)
                or proposal.get("run_id") != run_id
                or not isinstance(approval, dict)
                or approval.get("run_id") != run_id
            ):
                return {"ok": False, "reason_code": "OPTIMIZATION_RUN_MISMATCH", "run_id": run_id}
            knowledge_sync = self._owned_knowledge_sync(run_id)
            if isinstance(knowledge_sync, dict):
                return knowledge_sync
            return self.run_summary(knowledge_sync).apply(proposal_id, approval_id)
        return {"ok": False, "reason_code": "OPTIMIZATION_OPERATION_INVALID", "run_id": run_id}

    def preflight(self, project: str, *, probes: dict[str, Any] | None = None) -> dict[str, Any]:
        """Query configured dependencies without creating run state or remote intents."""
        if not isinstance(project, str) or not _PROJECT_ID.fullmatch(project):
            return {
                "ready": False, "status": "PROJECT_NOT_READY",
                "reason_code": "PROJECT_NOT_READY", "project": project,
                "missing": ["project"], "components": {},
            }
        configured = profile_path(project, self.config_root)
        loaded = load_profile(configured)
        if not loaded.get("ready"):
            return {
                **loaded,
                "ready": False,
                "status": "PROJECT_NOT_READY",
                "reason_code": "PROJECT_NOT_READY",
                "project": project,
                "components": {},
            }
        if loaded.get("profile", {}).get("project_id") != project:
            return {
                "ready": False,
                "status": "PROJECT_NOT_READY",
                "reason_code": "PROJECT_PROFILE_MISMATCH",
                "project": project,
                "components": {},
            }
        from preflight import live_probes, run_preflight

        selected = probes if probes is not None else live_probes()
        profile_hash = hashlib.sha256(configured.read_bytes()).hexdigest()
        return run_preflight(project, loaded["profile"], profile_hash, selected)

    def trace(self, run_id: str) -> dict[str, Any]:
        """Return the local durable run trace for audit and fake end-to-end assertions."""
        events = self.state.events(run_id)
        if not events:
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        intake = events[0]["payload"]
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        profile = pinned["profile"]
        ku = next(
            (item for item in profile.get("knowledge_sources", []) if item.get("provider") == "ku"),
            {},
        )
        collaboration = [
            result for result in self.state.external_results(run_id)
            if result.get("intent", {}).get("operation", "").startswith(
                ("collaboration.", "infoflow.group.")
            )
        ]
        role_routing = []
        for event in events:
            evidence = event.get("payload", {}).get("evidence")
            category = evidence.get("category") if isinstance(evidence, dict) else None
            if isinstance(category, str):
                role_routing.append({
                    "category": category,
                    "roles": _roles_for_category(category),
                })
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": run_id,
            "project": {
                "project_id": intake["project"],
                "profile_hash": intake["profile_hash"],
                "ku_repo_id": ku.get("repo_id"),
                "ku_parent_doc_id": ku.get("parent_doc_id"),
            },
            "events": events,
            "artifacts": self.artifacts.artifacts_for_run(run_id),
            "collaboration": collaboration,
            "role_routing": role_routing,
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        return self.recovery.resume(run_id)

    def phase_protocol(self, knowledge_sync: Any | None = None) -> PhaseProtocol:
        return PhaseProtocol(
            state_store=self.state,
            artifact_store=self.artifacts,
            knowledge_sync=knowledge_sync,
            approval_ledger=self.approvals,
            evidence_gate=self.evidence_gate,
            transition_policy=self.transition_policy,
        )

    def run_summary(self, knowledge_sync: Any | None = None) -> Any:
        """Return the owned G10 summary boundary without exposing mutable stores to callers."""
        from run_summary import RunSummary

        return RunSummary(
            self.state,
            self.artifacts,
            self.approvals,
            knowledge_sync=knowledge_sync,
            control_root=self._run_summary_options.get("control_root"),
            validation_runner=self._run_summary_options.get("validation_runner"),
        )

    def knowledge_sync(
        self,
        run_id: str,
        *,
        ku_transport: Any | None = None,
        cafe_transport: Any | None = None,
        username: str | None = None,
        cafe_preflight: bool = True,
    ) -> KnowledgeSync | dict[str, Any]:
        events = self.state.events(run_id)
        if not events:
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        intake = events[0].get("payload")
        if not isinstance(intake, dict):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        recorded_path = intake.get("profile_path")
        recorded_project = intake.get("project")
        # The pin can have moved since INTAKE: `repin-profile` accepts an edited profile
        # under its own approval, and `_runtime_profile` already reads the moved pin.
        # Reading INTAKE's hash here instead left the run healthy through one door and
        # PROFILE_CONFLICT through the other, which blocked every phase completion after
        # a legitimate re-pin.
        recorded_hash = _pinned_profile_hash(self, run_id, events)
        card_id = intake.get("requirement_id")
        if (
            not isinstance(recorded_path, str)
            or not isinstance(recorded_project, str)
            or not recorded_project
            or not isinstance(recorded_hash, str)
            or not recorded_hash
            or not isinstance(card_id, str)
            or not card_id
        ):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        if recorded_path not in acceptable_profile_paths(
            recorded_project, card_id, self.config_root
        ):
            return {"ok": False, "reason_code": "PROJECT_PROFILE_PATH_MISMATCH", "run_id": run_id}
        try:
            current_hash = hashlib.sha256(Path(recorded_path).read_bytes()).hexdigest()
        except OSError:
            current_hash = None
        if current_hash is not None and current_hash != recorded_hash:
            return {
                "ok": False,
                "reason_code": "PROFILE_CONFLICT",
                "run_id": run_id,
                "profile_path": recorded_path,
            }
        profile_result = load_profile(recorded_path)
        if not profile_result.get("ready"):
            return {"ok": False, "run_id": run_id, **profile_result}
        if profile_result.get("profile", {}).get("project_id") != recorded_project:
            return {"ok": False, "reason_code": "PROJECT_PROFILE_MISMATCH", "run_id": run_id}
        try:
            return KnowledgeSync.from_profile(
                profile_result["profile"],
                run_id=run_id,
                card_id=card_id,
                state_store=self.state,
                ku_transport=ku_transport,
                cafe_transport=cafe_transport,
                username=username,
                cafe_preflight=cafe_preflight,
            )
        except ValueError as exc:
            return {"ok": False, "reason_code": str(exc), "run_id": run_id}

    def _owned_knowledge_sync(self, run_id: str) -> KnowledgeSync | dict[str, Any]:
        allowed_options = {"ku_transport", "cafe_transport", "username", "cafe_preflight"}
        if set(self._knowledge_sync_transport_options) - allowed_options:
            return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_MISMATCH", "run_id": run_id}
        sync = self.knowledge_sync(run_id, **dict(self._knowledge_sync_transport_options))
        if isinstance(sync, dict):
            return sync
        events = self.state.events(run_id)
        intake = events[0].get("payload") if events else None
        pinned = self._runtime_profile(run_id)
        if not isinstance(intake, dict) or not pinned.get("ok"):
            return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_MISMATCH", "run_id": run_id}
        ku_target = project_ku_target(pinned["profile"])
        if (
            ku_target is None
            or not isinstance(sync, KnowledgeSync)
            or sync.state is not self.state
            or sync.run_id != run_id
            or sync.card_id != intake.get("requirement_id")
            or sync.project_parent_doc_id != ku_target[1]
            or sync.parent_doc_id is not None
        ):
            return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_MISMATCH", "run_id": run_id}
        return sync

    def task_business_module(self, run_id: str, task_id: Any) -> str | None:
        """The business repo module a DAG task targets, read from the pinned TASKS artifact.

        The task-dag node names its business repository explicitly (MEDIUM-005), so a
        cross-repo run binds each task's WORKSPACE to the repo that task actually changes
        rather than to business_repos[0]. Returns None for a legacy DAG whose nodes carry no
        business_module, or when the task/DAG is not resolvable — callers fall back to the
        single-repo case only when it is unambiguous."""
        if not isinstance(task_id, str) or not task_id:
            return None
        dag = self.artifacts.latest_phase(run_id, "TASKS", None)
        if not dag.get("valid"):
            return None
        content = (dag.get("envelope") or {}).get("content") or {}
        for node in content.get("nodes") or []:
            if isinstance(node, dict) and node.get("task_id") == task_id:
                module = node.get("business_module")
                return module if isinstance(module, str) and module else None
        return None

    def workspace_binding(
        self, run_id: str, workspace_receipts: dict[str, Any]
    ) -> dict[str, Any]:
        """Derive the G4 identity from the durable task frontier and owned worktrees."""
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        if current["state"] != "WORKSPACE":
            return {
                "ok": False, "reason_code": "WORKSPACE_STATE_REQUIRED",
                "run_id": run_id, "state": current["state"],
            }
        action = self.phase_protocol().next(run_id)
        if (
            not action.get("ok")
            or action.get("state") != "WORKSPACE"
            or action.get("controller") != "workspace"
            or not isinstance(action.get("task_id"), str)
            or not action["task_id"]
        ):
            return {
                "ok": False,
                "reason_code": action.get("reason_code", "TASK_FRONTIER_EMPTY"),
                "run_id": run_id,
            }
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        if not isinstance(workspace_receipts, dict) or set(workspace_receipts) != {"business", "tests"}:
            return {"ok": False, "reason_code": "WORKSPACE_BINDING_REQUIRED", "run_id": run_id}

        profile = pinned["profile"]
        business_repositories = profile.get("business_repos")
        test_repository = profile.get("test_repo")
        if not isinstance(business_repositories, list) or not isinstance(test_repository, dict):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        canonical_repositories: dict[str, dict[str, str]] = {}
        task_id = action["task_id"]
        for role in ("business", "tests"):
            receipt = workspace_receipts.get(role)
            if not isinstance(receipt, dict):
                return {"ok": False, "reason_code": "WORKSPACE_BINDING_REQUIRED", "run_id": run_id}
            required = {
                "role", "module", "repo_path", "worktree_path", "baseline_revision", "owner_token",
            }
            if set(receipt) != required or receipt.get("role") != role or any(
                not isinstance(receipt.get(field), str) or not receipt[field]
                for field in required - {"role"}
            ):
                return {"ok": False, "reason_code": "WORKSPACE_RECEIPT_INVALID", "run_id": run_id}
            candidates = business_repositories if role == "business" else [test_repository]
            matched = [
                repository for repository in candidates
                if isinstance(repository, dict)
                and repository.get("module") == receipt["module"]
                and _same_path(repository.get("path"), receipt["repo_path"])
            ]
            if len(matched) != 1:
                return {
                    "ok": False, "reason_code": "WORKSPACE_PROFILE_MISMATCH",
                    "run_id": run_id, "role": role,
                }
            if role == "business":
                # The receipt must be for the repo THIS task targets, not merely a registered
                # business repo (MEDIUM-005). A single-repo profile is unambiguous, so an
                # advisory/mismatched module is tolerated; a multi-repo profile is not.
                declared = self.task_business_module(run_id, task_id)
                if (
                    declared
                    and len(business_repositories) > 1
                    and receipt["module"] != declared
                ):
                    return {
                        "ok": False, "reason_code": "WORKSPACE_TASK_REPO_MISMATCH",
                        "run_id": run_id, "role": role,
                        "task_module": declared, "receipt_module": receipt["module"],
                    }
            try:
                ownership = self.workspaces.query_ownership(
                    receipt["repo_path"], run_id, task_id, receipt["owner_token"]
                )
            except Exception:
                return {
                    "ok": False, "reason_code": "WORKTREE_OWNERSHIP_QUERY_FAILED",
                    "run_id": run_id, "role": role,
                }
            if (
                ownership.get("status") != "VERIFIED"
                or ownership.get("ownership_status") != "ACTIVE"
                or ownership.get("run_id") != run_id
                or ownership.get("task_id") != task_id
                or not _same_path(ownership.get("repo_path"), receipt["repo_path"])
                or not _same_path(ownership.get("worktree_path"), receipt["worktree_path"])
                or ownership.get("baseline_revision") != receipt["baseline_revision"]
                or ownership.get("owner_token") != receipt["owner_token"]
            ):
                return {
                    "ok": False,
                    "reason_code": ownership.get("reason_code") or "WORKTREE_NOT_OWNED",
                    "run_id": run_id, "role": role,
                }
            canonical_repositories[role] = {
                "role": role,
                "module": receipt["module"],
                "repo_path": str(Path(receipt["repo_path"]).expanduser().resolve()),
                "worktree_path": str(Path(receipt["worktree_path"]).expanduser().resolve()),
                "baseline_revision": receipt["baseline_revision"],
                "owner_proof": hashlib.sha256(receipt["owner_token"].encode("utf-8")).hexdigest(),
            }

        binding = {
            "schema_version": "1",
            "run_id": run_id,
            "source_event_id": action["source_event_id"],
            "task_id": task_id,
            "profile_hash": pinned["profile_hash"],
            "repositories": canonical_repositories,
            "source_revisions": {
                "business": canonical_repositories["business"]["baseline_revision"],
                "tests": canonical_repositories["tests"]["baseline_revision"],
            },
        }
        input_hash = _canonical_hash({"gate": "G4", "workspace_binding": binding})
        return {
            "ok": True, "reason_code": "OK", "run_id": run_id,
            "input_hash": input_hash, "workspace_binding": binding,
        }

    @guard_execution
    def advance(
        self,
        run_id: str,
        next_state: str,
        evidence_context: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return current
        recovery_block = self._recovery_block(run_id, current["state"])
        if recovery_block is not None:
            return recovery_block
        current_state = current["state"]
        bound_workspace = None
        effective_evidence = dict(evidence_context)
        if current_state == "WORKSPACE" and next_state == "PLAN":
            if effective_evidence.get("blocking_findings"):
                return {
                    "run_id": run_id,
                    "state": current_state,
                    "passed": False,
                    "action": next_state,
                    "reason_code": "BLOCKING_FINDING",
                    "missing_evidence": [],
                }
            receipts = effective_evidence.get("workspace_receipts")
            supplied_aliases = any(
                key in effective_evidence
                for key in ("task_id", "source_revisions", "workspace_binding")
            )
            if receipts is None:
                if supplied_aliases:
                    return {
                        "run_id": run_id, "state": current_state,
                        "reason_code": "WORKSPACE_BINDING_REQUIRED",
                    }
            else:
                derived = self.workspace_binding(run_id, receipts)
                if not derived.get("ok"):
                    return {"run_id": run_id, "state": current_state, **derived}
                bound_workspace = derived["workspace_binding"]
                if effective_evidence.get("input_hash") != derived["input_hash"]:
                    return {
                        "run_id": run_id, "state": current_state,
                        "reason_code": "WORKSPACE_INPUT_HASH_MISMATCH",
                    }
                if "task_id" in effective_evidence and effective_evidence["task_id"] != bound_workspace["task_id"]:
                    return {
                        "run_id": run_id, "state": current_state,
                        "reason_code": "WORKSPACE_TASK_MISMATCH",
                    }
                if (
                    "workspace_binding" in effective_evidence
                    and effective_evidence["workspace_binding"] != bound_workspace
                ):
                    return {
                        "run_id": run_id, "state": current_state,
                        "reason_code": "WORKSPACE_BINDING_MISMATCH",
                    }
                for key in ("source_revisions", "repo_revisions", "evidence_revisions"):
                    if key in effective_evidence and effective_evidence[key] != bound_workspace["source_revisions"]:
                        return {
                            "run_id": run_id, "state": current_state,
                            "reason_code": "WORKSPACE_REVISION_MISMATCH",
                        }

            required_artifacts = list(requirement_for("PLAN", "WORKSPACE").artifacts)
            supplied_artifacts = effective_evidence.get("artifacts")
            if not isinstance(supplied_artifacts, list):
                missing_artifacts = required_artifacts
            else:
                missing_artifacts = [
                    artifact for artifact in required_artifacts
                    if artifact not in supplied_artifacts
                ]
            if missing_artifacts:
                return {
                    "run_id": run_id,
                    "state": current_state,
                    "passed": False,
                    "action": next_state,
                    "reason_code": "MISSING_ARTIFACT",
                    "missing_evidence": missing_artifacts,
                }

            projected_evidence = {
                "input_hash": effective_evidence.get("input_hash"),
                "approval_id": effective_evidence.get("approval_id"),
                "artifacts": required_artifacts,
            }
            if bound_workspace is not None:
                revisions = dict(bound_workspace["source_revisions"])
                projected_evidence.update({
                    "task_id": bound_workspace["task_id"],
                    "source_revisions": revisions,
                    "repo_revisions": dict(revisions),
                    "evidence_revisions": dict(revisions),
                    "workspace_binding": bound_workspace,
                })
            effective_evidence = projected_evidence

        input_hash = effective_evidence.get("input_hash")
        canonical_input_hash = input_hash if isinstance(input_hash, str) else ""

        retry_identity = self._retry_operation_identity(current, next_state, canonical_input_hash)
        if retry_identity is not None:
            existing = self.state.idempotency_result(
                self._advance_idempotency_key(run_id, retry_identity)
            )
            if existing is not None:
                return existing

        operation_identity = self._advance_operation_identity(
            current["events"][-1]["event_id"], current_state, next_state, canonical_input_hash
        )
        idempotency_key = self._advance_idempotency_key(run_id, operation_identity)
        existing = self.state.idempotency_result(idempotency_key)
        if existing is not None:
            return existing
        transition = self.transition_policy.validate(current_state, next_state)
        if not transition["allowed"]:
            return {"run_id": run_id, "state": current_state, **transition}

        if next_state == "RELEASE_SUCCESS":
            # Artifact names and matching caller-provided fingerprints cannot prove
            # publication. Only release ingestion validates every frozen module's
            # owned platform receipt and atomically records success with that evidence.
            return {"run_id": run_id, "state": current_state,
                    "reason_code": "RELEASE_INGEST_REQUIRED"}

        gate_context = self._ledger_backed_evidence(run_id, current_state, next_state, effective_evidence)
        gate = self.evidence_gate.check(next_state, gate_context, current_state)
        if not gate["passed"]:
            return {"run_id": run_id, "state": current_state, **gate}

        transition_payload = {
            "previous_state": current_state,
            "input_hash": canonical_input_hash,
            "evidence": gate_context,
            "policy_decision": transition,
            "operation_identity": operation_identity,
            **(
                {"task_id": gate_context["task_id"]}
                if isinstance(gate_context.get("task_id"), str) and gate_context["task_id"]
                else {}
            ),
            **(
                {"source_revisions": dict(gate_context["source_revisions"])}
                if _valid_source_revisions(gate_context.get("source_revisions"))
                and not (current_state == "WORKSPACE" and next_state == "PLAN" and bound_workspace is None)
                else {}
            ),
            **({"workspace_binding": bound_workspace} if bound_workspace is not None else {}),
        }
        result = {
            "run_id": run_id,
            "state": next_state,
            "reason_code": "OK",
        }
        committed = self.state.commit_transition_result(
            run_id, current["events"][-1]["event_id"], next_state,
            transition_payload, idempotency_key, result,
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"run_id": run_id, "state": current_state, "reason_code": "ADVANCE_CONFLICT"}
        return {"run_id": run_id, "state": current_state, "reason_code": "STALE_ACTION"}

    @guard_execution
    def route_failure(
        self,
        run_id: str,
        reason_code: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return current
        # A PARTIAL submission routes to DIAGNOSE with its AMBIGUOUS/UNKNOWN repositories
        # still open: diagnosis is how a person gets to decide them. Only those intents of
        # this exact SUBMIT_PARTIAL evidence are let through; nothing is released.
        held = frozenset()
        if reason_code == "SUBMIT_PARTIAL" and current["state"] == "SUBMIT":
            held = frozenset(
                row["intent_id"] for row in
                ((evidence.get("submission_set") or {}).get("repositories") or [])
                if isinstance(row, dict) and isinstance(row.get("intent_id"), str))
        recovery_block = self._recovery_block(run_id, current["state"], held_for_diagnosis=held)
        if recovery_block is not None:
            return recovery_block
        routed_evidence = dict(evidence)
        collaboration_category = _runtime_failure_category(
            reason_code, routed_evidence.get("classification")
        )
        routed_evidence["category"] = collaboration_category
        identity = json.dumps(routed_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idempotency_key = f"failure:{run_id}:{reason_code}:{hashlib.sha256(identity.encode()).hexdigest()}"
        existing = self.state.idempotency_result(idempotency_key)
        if existing is not None:
            return existing
        next_state = (
            "DIAGNOSE"
            if _is_runtime_failure(reason_code)
            else self.transition_policy.failure_target(reason_code)
        )
        transition = self.transition_policy.validate(current["state"], next_state)
        if not transition["allowed"]:
            return {
                "run_id": run_id,
                "state": current["state"],
                **transition,
            }

        result = {
            "run_id": run_id,
            "state": next_state,
            "reason_code": reason_code,
            "collaboration_category": collaboration_category,
        }
        committed = self.state.commit_transition_result(
            run_id, current["events"][-1]["event_id"], next_state,
            {"reason_code": reason_code, "evidence": routed_evidence, "policy_decision": transition},
            idempotency_key, result,
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            self._record_failure_case(run_id, routed_evidence)
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"run_id": run_id, "state": current["state"], "reason_code": "FAILURE_CONFLICT"}
        return {"run_id": run_id, "state": current["state"], "reason_code": "STALE_ACTION"}

    def _record_failure_case(self, run_id: str, evidence: dict[str, Any]) -> None:
        """Record this failure's signature in the cross-run FailureCase library (best-effort
        — a library write must never fail the failure routing itself)."""
        signature = evidence.get("failure_signature") or evidence.get("signature")
        if not isinstance(signature, str) or not signature:
            return
        try:
            self.state.record_failure_case(
                signature, evidence.get("classification"), run_id, resolved=False
            )
        except Exception:
            # Best-effort: the FailureCase library must never fail the failure routing. But a
            # silently dropped write means the cross-run recurrence count is wrong, so it is
            # logged rather than swallowed (MEDIUM-004).
            _log.warning(
                "failure-case write failed for run %s signature %s (best-effort)",
                run_id, signature, exc_info=True,
            )

    def stop(self, run_id: str) -> dict[str, Any]:
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return current
        transition = self.transition_policy.validate_stop(current["state"])
        if not transition["allowed"]:
            return {"run_id": run_id, "state": current["state"], **transition}
        event = self.state.transition(
            run_id,
            "STOPPED",
            {"previous_state": current["state"], "policy_decision": transition},
        )
        return {"run_id": run_id, "state": "STOPPED", "event_id": event["event_id"]}

    def _ledger_backed_evidence(
        self, run_id: str, current_state: str, action: str, evidence_context: dict[str, Any]
    ) -> dict[str, Any]:
        context = dict(evidence_context)
        context["run_id"] = run_id
        requirement = requirement_for(action, current_state)
        if requirement.approval_gate:
            approval_id = context.get("approval_id")
            approval = self.approvals.get(approval_id) if isinstance(approval_id, str) else None
            context["approval_record"] = approval
            if approval is not None:
                context["approved_input_hash"] = approval["input_hash"]
        if requirement.requires_environment:
            # The gate only compares the two fingerprints for equality, so a caller could
            # pass two identical-but-stale values and pass a mandatory-environment gate
            # (e.g. RELEASE) on the generic advance path, which does not go through the
            # frozen plan's environment binding. Pin the expected side to the run's
            # profile so the evidence must match the actual pinned environment, not merely
            # itself. A profile that cannot be loaded leaves it unset -> fails closed as
            # MISSING_ENVIRONMENT_EVIDENCE rather than passing.
            from pipeline_plan import canonical_hash
            pinned = self._runtime_profile(run_id)
            profile = pinned.get("profile") if pinned.get("ok") else None
            environment_profile = profile.get("environment_profile") if isinstance(profile, dict) else None
            context["environment_fingerprint"] = (
                canonical_hash(environment_profile) if environment_profile is not None else None
            )
        return context

    def _recovery_block(
        self, run_id: str, current_state: str, *, held_for_diagnosis: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        recovery = self.recovery.resume(run_id, held_for_diagnosis=held_for_diagnosis)
        if recovery["retry_allowed"]:
            return None
        return {
            "run_id": run_id,
            "state": current_state,
            "reason_code": "RECOVERY_REQUIRED",
            "retry_allowed": False,
            "actions": recovery["actions"],
            "recovery": recovery,
        }

    def _advance_operation_identity(
        self, source_event_id: str, source_state: str, target_state: str, input_hash: str
    ) -> dict[str, str | None]:
        return {
            "source_event_id": source_event_id,
            "source_state": source_state,
            "target_state": target_state,
            "input_hash": input_hash,
            "approval_gate": requirement_for(target_state, source_state).approval_gate,
        }

    def _retry_operation_identity(
        self, current: dict[str, Any], target_state: str, input_hash: str
    ) -> dict[str, str | None] | None:
        events = current["events"]
        if not events:
            return None
        last_event = events[-1]
        identity = last_event["payload"].get("operation_identity")
        if not isinstance(identity, dict) or last_event["state"] != target_state:
            return None
        expected_keys = {
            "source_event_id",
            "source_state",
            "target_state",
            "input_hash",
            "approval_gate",
        }
        if set(identity) != expected_keys or identity.get("target_state") != target_state:
            return None
        if identity.get("input_hash") != input_hash:
            return None
        source_event_id = identity.get("source_event_id")
        source_state = identity.get("source_state")
        approval_gate = identity.get("approval_gate")
        if (
            not isinstance(source_event_id, str)
            or not isinstance(source_state, str)
            or not isinstance(approval_gate, (str, type(None)))
        ):
            return None
        return {
            "source_event_id": source_event_id,
            "source_state": source_state,
            "target_state": target_state,
            "input_hash": input_hash,
            "approval_gate": approval_gate,
        }

    def _advance_idempotency_key(
        self, run_id: str, operation_identity: dict[str, str | None]
    ) -> str:
        identity = json.dumps(operation_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"advance:{run_id}:{hashlib.sha256(identity.encode()).hexdigest()}"

    def approve(
        self,
        approval_id: str,
        decision: str,
        input_hash: str,
        channel: str,
        run_id: str | None = None,
        responder: str | None = None,
    ) -> dict[str, Any]:
        bound_run = run_id
        record = self.approvals.get(approval_id)
        if bound_run is None:
            bound_run = record.get("run_id") if isinstance(record, dict) else None
        # A drifted run may still answer the gate that authorizes its migration.
        # Without this the card could be delivered but its decision never landed,
        # leaving `migrate-workflow-spec --approval-id` permanently APPROVAL_REQUIRED.
        if not migration_exempt(record.get("action") if isinstance(record, dict) else None):
            blocked = execution_guard(self.state, bound_run)
            if blocked is not None:
                return blocked
        result = self.approvals.resolve(
            approval_id, decision, input_hash, channel,
            run_id=run_id, responder=responder, state_store=self.state,
        )
        # A direct Comate approval must create the same durable handoff as an
        # Infoflow approval.  Previously only ApprovalWatcher created it, so a
        # decision made through the IDE was recorded in the ledger but had no
        # resume signal for the next Comate turn.
        if (
            result.get("reason_code") == "OK"
            and result.get("effective_decision") == "APPROVE"
            and result.get("effective_channel") == channel
            and isinstance(bound_run, str)
        ):
            latest = self.state.events(bound_run)
            if latest:
                handoff_id = f"approval-resume-{approval_id}-{latest[-1]['event_id']}"
                handoff = self.state.record_handoff(
                    bound_run,
                    handoff_id,
                    {
                        "kind": "APPROVAL_RESUME",
                        "run_id": bound_run,
                        "event_id": latest[-1]["event_id"],
                        "approval_id": approval_id,
                        "input_hash": input_hash,
                        "action": result.get("action"),
                    },
                )
                result["handoff"] = handoff
        return result

    def collaboration_session(self, group_client: Any) -> CollaborationSession:
        """Create the per-run collaboration adapter with an explicitly supplied boundary."""
        def progress_provider(run_id: str) -> dict[str, Any]:
            from progress_snapshot import build as build_progress

            return build_progress(self, run_id)

        return CollaborationSession(
            self.state, group_client, approvals=self.approvals,
            progress_provider=progress_provider,
        )

    def icode_runtime(self, run_id: str, **options: Any) -> Any:
        if self.status(run_id)["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        if set(options) & {"state_store", "approval_ledger", "artifact_store", "workspace_manager", "run_id", "profile_hash"}:
            return {"ok": False, "reason_code": "RUNTIME_OPTION_FORBIDDEN", "run_id": run_id}
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        policy = pinned["profile"].get("submission_policy") or "one_cr_per_change_set"
        if options.get("submission_policy") not in (None, policy):
            return {"ok": False, "reason_code": "PROFILE_CONFLICT", "run_id": run_id}
        options["submission_policy"] = policy
        from clients.icode_runtime import IcodeRuntime

        return IcodeRuntime(
            state_store=self.state,
            approval_ledger=self.approvals,
            artifact_store=self.artifacts,
            workspace_manager=self.workspaces,
            run_id=run_id,
            profile_hash=pinned["profile_hash"],
            **options,
        )

    def ai_review_runtime(self, run_id: str, **options: Any) -> Any:
        """The platform's own review (小码哥), bound to this run's ledger.

        Separate from `icode_runtime` on purpose: this boundary writes no code and
        needs no approval, but its conversation id is unrecoverable once lost, so it
        owns an intent of its own rather than riding on the submission's.
        """
        if self.status(run_id)["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        if set(options) & {"state_store", "run_id"}:
            return {"ok": False, "reason_code": "RUNTIME_OPTION_FORBIDDEN", "run_id": run_id}
        from clients.icode_ai_review import IcodeAiReview

        return IcodeAiReview(
            state_store=self.state,
            artifact_store=self.artifacts,
            run_id=run_id,
            **options,
        )

    def submit_to_ipipe(
        self,
        run_id: str,
        change_set: dict[str, Any],
        approval: dict[str, Any],
        *,
        icode_runtime: Any,
        change_set_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit through the owned iCode boundary and form the IPIPE checkpoint.

        `change_set_ids` lists every repository of the attempt (contract 3.2); the
        checkpoint is formed only when all of them are confirmed.
        """
        # This receipt is a pure return. Everything else, including runtime
        # preflight, must wait for a matching workflow policy before executing.
        if isinstance(change_set, dict) and isinstance(approval, dict):
            identifiers = [change_set.get(key) for key in (
                "change_set_id", "revision_set_id", "input_hash")]
            if all(isinstance(value, str) and value for value in identifiers):
                existing = self.state.idempotency_result(
                    f"submit-to-ipipe:{run_id}:{identifiers[0]}:{identifiers[1]}")
                if existing is not None:
                    return existing
        blocked = execution_guard(self.state, run_id)
        if blocked is not None:
            return blocked
        try:
            return self._submit_to_ipipe(run_id, change_set, approval, icode_runtime,
                                         change_set_ids)
        except Exception:
            return {"ok": False, "reason_code": "SUBMISSION_BINDING_FAILED", "run_id": run_id}

    def _submit_to_ipipe(
        self,
        run_id: str,
        change_set: Any,
        approval: Any,
        icode_runtime: Any,
        change_set_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(change_set, dict) or not isinstance(approval, dict):
            return {"ok": False, "reason_code": "INVALID_INPUT", "run_id": run_id}
        change_set_id = change_set.get("change_set_id")
        revision_set_id = change_set.get("revision_set_id")
        input_hash = change_set.get("input_hash")
        if not all(
            isinstance(value, str) and value
            for value in (change_set_id, revision_set_id, input_hash)
        ):
            return {"ok": False, "reason_code": "CHANGE_SET_INVALID", "run_id": run_id}
        result_key = f"submit-to-ipipe:{run_id}:{change_set_id}:{revision_set_id}"
        existing = self.state.idempotency_result(result_key)
        if existing is not None:
            return existing
        current = self.status(run_id)
        if current.get("state") != "SUBMIT":
            return {
                "ok": False, "reason_code": "INVALID_STATE", "run_id": run_id,
                "state": current.get("state"),
            }
        if getattr(icode_runtime, "run_id", None) != run_id or not callable(
            getattr(icode_runtime, "submit", None)
        ):
            return {"ok": False, "reason_code": "ICODE_RUNTIME_MISMATCH", "run_id": run_id}

        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        # Contract 3.2: a caller list that omits a required repository is refused before
        # anything is pushed, not after the primary CR exists.
        required = _attempt_change_set_ids(self, run_id, change_set)
        if change_set_ids is not None and not set(required) <= set(change_set_ids):
            return {"ok": False, "reason_code": "SUBMISSION_SET_MISMATCH", "run_id": run_id,
                    "required": required, "given": sorted(change_set_ids)}
        submitted = icode_runtime.submit(change_set, approval)
        if not isinstance(submitted, dict) or submitted.get("reason_code") != "OK" or not submitted.get("ok"):
            return submitted if isinstance(submitted, dict) else {
                "ok": False, "reason_code": "ICODE_RECEIPT_INVALID", "run_id": run_id,
            }
        # The receipt names the intent lineage it landed under. An attempt that
        # supersedes an abandoned one lives under a chained key, so the base key alone
        # would look for a record that is not there; walking the abandoned lineage finds
        # it for receipts written before the key was reported.
        submit_key = submitted.get("submit_key") or _live_submit_key(
            self.state, f"icode.submit:{run_id}:{change_set_id}:{revision_set_id}"
        )
        durable = self.state.result_by_idempotency_key(submit_key)
        if (
            not isinstance(durable, dict)
            or durable.get("operation") != "icode.submit"
            or durable.get("receipt", {}).get("response") != submitted
        ):
            return {"ok": False, "reason_code": "ICODE_RECEIPT_REQUIRED", "run_id": run_id}
        intent = durable.get("intent", {}).get("payload")
        approval_id = approval.get("approval_id")
        approval_input_hash = approval.get("input_hash")
        repo_path = change_set.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path:
            return {"ok": False, "reason_code": "CHANGE_SET_INVALID", "run_id": run_id}
        expected_intent = {
            "run_id": run_id,
            "change_set_id": change_set_id,
            "revision_set_id": revision_set_id,
            "input_hash": input_hash,
            "approval_id": approval_id,
            "repo_path": str(Path(repo_path).expanduser().resolve()),
            "module": change_set.get("module"),
            "target_branch": change_set.get("target_branch"),
            "commit_revision": change_set.get("commit_revision"),
            "card_id": change_set.get("card_id"),
            "owner": change_set.get("owner"),
            "revision_set": change_set.get("revision_set"),
        }
        if change_set.get("task_id"):
            expected_intent["task_id"] = change_set["task_id"]
        if intent != expected_intent and not _replayed_submit_intent(
            self.approvals, run_id, intent, expected_intent
        ):
            return {"ok": False, "reason_code": "ICODE_RECEIPT_MISMATCH", "run_id": run_id}
        approval_record = self.approvals.get(approval_id) if isinstance(approval_id, str) else None
        if not isinstance(approval_record, dict):
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id}
        if approval_record.get("run_id") != run_id or approval_record.get("run_id") == "legacy":
            return {"ok": False, "reason_code": "APPROVAL_RUN_MISMATCH", "run_id": run_id}
        if gate_of(approval_record.get("action")) != "G7":
            return {"ok": False, "reason_code": "APPROVAL_GATE_MISMATCH", "run_id": run_id}
        if approval_input_hash != input_hash or approval_record.get("input_hash") != input_hash:
            return {"ok": False, "reason_code": "APPROVAL_INPUT_MISMATCH", "run_id": run_id}
        if approval_record.get("effective_decision") != "APPROVE":
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id}

        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        events = current["events"]
        intake = events[0]["payload"]
        profile = pinned["profile"]
        binding_error, controller_binding = _submission_controller_binding(
            profile, submitted, change_set
        )
        if binding_error is not None:
            return {"ok": False, "reason_code": binding_error, "run_id": run_id}
        # Contract 3.2: the IPIPE checkpoint follows only a SUBMITTED attempt -- every
        # repository listed for it confirmed with a CR. Checked here, at the boundary that
        # owns the transition, not by the caller's ordering.
        import submission_set
        aggregate = submission_set.evaluate(
            self.state, run_id, sorted(set(required) | set(change_set_ids or [])),
            revision_set_id, change_set_id)
        if aggregate["status"] != submission_set.SUBMITTED:
            return {"ok": False, "reason_code": "SUBMISSION_SET_INCOMPLETE", "run_id": run_id,
                    "retry_allowed": aggregate["status"] == submission_set.PENDING,
                    "submission_set": aggregate}
        content = json.dumps(
            submitted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        submission = self.artifacts.put(run_id, "submission", content, {
            "controller_binding": controller_binding,
            "icode_intent_id": durable["intent"]["intent_id"],
            "change_set_id": change_set_id,
            "revision_set_id": revision_set_id,
        })
        # A requirement that spans repositories has one reviewed change set per
        # repository, and every one of them has to reach iCode before the pipelines
        # are asked anything: a build over half the change is not evidence about the
        # change. So the transition waits for the last submission instead of firing on
        # the first, and the submissions in between are recorded and replayable.
        outstanding = _outstanding_submissions(self, run_id)
        followup = _submit_followup_state(self, run_id)
        if followup == "SUBMIT":
            recorded = {
                "ok": True, "reason_code": "SUBMISSION_RECORDED", "run_id": run_id,
                "state": "SUBMIT",
                "submission_artifact_id": submission["artifact_id"],
                "submission_hash": submission["sha256"],
                "outstanding_tasks": outstanding,
            }
            self.state.save_idempotency_result(result_key, recorded)
            return recorded
        if followup == "WORKSPACE":
            # This task is in iCode, but open DAG nodes still owe a Review.
            # Staying in SUBMIT would park the frontier; jumping to IPIPE
            # would build a half-written requirement.
            transition = self.transition_policy.validate("SUBMIT", "WORKSPACE")
            if not transition.get("allowed"):
                return {"run_id": run_id, "state": "SUBMIT", **transition, "ok": False}
            workspace_payload = {
                "previous_state": "SUBMIT",
                "outstanding_tasks": outstanding,
                "submission_artifact_id": submission["artifact_id"],
                "submission_hash": submission["sha256"],
                "policy_decision": transition,
            }
            workspace_result = {
                "ok": True,
                "reason_code": "SUBMISSION_RECORDED",
                "submission_artifact_id": submission["artifact_id"],
                "submission_hash": submission["sha256"],
                "outstanding_tasks": outstanding,
            }
            committed_workspace = self.state.commit_transition_result(
                run_id,
                events[-1]["event_id"],
                "WORKSPACE",
                workspace_payload,
                result_key,
                workspace_result,
            )
            if committed_workspace.get("status") in {"COMMITTED", "REPLAY"}:
                return committed_workspace["result"]
            if committed_workspace.get("status") == "RESULT_CONFLICT":
                return {"ok": False, "reason_code": "SUBMISSION_CONFLICT", "run_id": run_id}
            return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}
        gate_context = self._ledger_backed_evidence(run_id, "SUBMIT", "IPIPE", {
            "input_hash": input_hash,
            "approval_id": approval_id,
            "artifacts": ["submission"],
        })
        gate = self.evidence_gate.check("IPIPE", gate_context, "SUBMIT")
        if not gate.get("passed"):
            return {"run_id": run_id, "state": "SUBMIT", **gate, "ok": False}
        transition = self.transition_policy.validate("SUBMIT", "IPIPE")
        if not transition.get("allowed"):
            return {"run_id": run_id, "state": "SUBMIT", **transition, "ok": False}
        from pipeline_plan import create_plan
        try:
            plan = create_plan(self, run_id, profile)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "run_id": run_id}
        submissions = plan["submissions"]
        primary = _primary_submission(profile, submissions, controller_binding, submission)
        primary_target = plan["modules"].get(primary["controller_binding"]["module"])
        if primary_target is None:
            primary_target = plan["modules"][plan["required_modules"][0]]
        payload = _ipipe_entry_payload(
            intake, plan, primary_target, approval_id, input_hash,
            extra={"policy_decision": transition},
        )
        committed = self.state.commit_transition_result(
            run_id,
            events[-1]["event_id"],
            "IPIPE",
            payload,
            result_key,
            {
                "ok": True,
                "reason_code": "OK",
                "submission_artifact_id": primary["artifact_id"],
                "submission_hash": primary["sha256"],
                "submissions": submissions,
            },
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return {"ok": False, "reason_code": "SUBMISSION_CONFLICT", "run_id": run_id}
        return {"ok": False, "reason_code": "STALE_ACTION", "run_id": run_id}

    def ipipe_runtime(self, run_id: str, api_transport: Any, **options: Any) -> Any:
        if self.status(run_id)["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        if set(options) & {"state_store", "approval_ledger", "run_id", "api_transport", "validated_profile", "profile_hash"}:
            return {"ok": False, "reason_code": "RUNTIME_OPTION_FORBIDDEN", "run_id": run_id}
        pinned = self._runtime_profile(run_id)
        if not pinned.get("ok"):
            return pinned
        from clients.ipipe_runtime import IpipeRuntime

        return IpipeRuntime(
            self.state,
            self.approvals,
            run_id,
            api_transport,
            validated_profile=pinned["profile"],
            profile_hash=pinned["profile_hash"],
            **options,
        )

    def _runtime_profile(self, run_id: str) -> dict[str, Any]:
        events = self.state.events(run_id)
        intake = events[0].get("payload") if events else None
        if not isinstance(intake, dict):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        recorded_path = intake.get("profile_path")
        recorded_hash = _pinned_profile_hash(self, run_id, events)
        recorded_project = intake.get("project")
        card_id = intake.get("requirement_id")
        if not all(isinstance(value, str) and value for value in (recorded_path, recorded_hash, recorded_project)):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        if recorded_path not in acceptable_profile_paths(
            recorded_project, card_id, self.config_root
        ):
            return {"ok": False, "reason_code": "PROJECT_PROFILE_PATH_MISMATCH", "run_id": run_id}
        try:
            is_requirement_profile = recorded_path == str(
                requirement_profile_path(recorded_project, card_id, self.config_root)
            )
        except ValueError:
            is_requirement_profile = False
        if is_requirement_profile:
            from profile_wizard import confirmation_hash, confirmed_inputs

            loaded_for_confirmation = load_profile(recorded_path, check_paths=False)
            confirmation = (
                loaded_for_confirmation.get("profile", {}).get("profile_confirmation")
                if loaded_for_confirmation.get("ready")
                else None
            )
            if not isinstance(confirmation, dict):
                return {
                    "ok": False,
                    "reason_code": "PROFILE_CONFIRMATION_REQUIRED",
                    "run_id": run_id,
                }
            if confirmation.get("confirmed_inputs_hash") != confirmation_hash(
                confirmed_inputs(loaded_for_confirmation["profile"])
            ):
                return {
                    "ok": False,
                    "reason_code": "PROFILE_CONFIRMATION_MISMATCH",
                    "run_id": run_id,
                }
        try:
            current_hash = hashlib.sha256(Path(recorded_path).read_bytes()).hexdigest()
        except OSError:
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        if current_hash != recorded_hash:
            return {"ok": False, "reason_code": "PROFILE_CONFLICT", "run_id": run_id}
        loaded = load_profile(recorded_path)
        if not loaded.get("ready"):
            return {"ok": False, "run_id": run_id, **loaded}
        if loaded.get("profile", {}).get("project_id") != recorded_project:
            return {"ok": False, "reason_code": "PROJECT_PROFILE_MISMATCH", "run_id": run_id}
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": run_id,
            "profile": loaded["profile"],
            "profile_hash": recorded_hash,
        }

    @guard_execution
    def request_infoflow_approval(
        self,
        run_id: str,
        action: str,
        input_hash: str,
        *,
        member_policy: dict[str, list[str]],
        deadline_at: str | None = None,
        evidence: dict[str, Any] | None = None,
        comate_client: Any,
        infoflow_client: Any,
        allow_workflow_spec_migration: bool = False,
    ) -> dict[str, Any]:
        if self.status(run_id)["state"] == "RUN_NOT_FOUND":
            return {"run_id": run_id, "reason_code": "RUN_NOT_FOUND"}
        request = self.approvals.request(
            action, input_hash, ["comate", "infoflow"], run_id=run_id,
            member_policy=member_policy, deadline_at=deadline_at,
        )
        payload = {
            "approval_id": request["approval_id"], "run_id": run_id, "action": action,
            "input_hash": input_hash, "deadline_at": request["deadline_at"],
            "evidence": evidence or {},
            "member_policy": request["member_policy"],
        }
        outcomes = []
        progress = None
        try:
            from progress_snapshot import build as build_progress

            progress = build_progress(self, run_id)
        except Exception:  # noqa: BLE001 - approval delivery remains usable without detail
            progress = None
        for channel, client in (("comate", comate_client), ("infoflow", infoflow_client)):
            envelope = {"channel": channel, "approval": payload}
            if channel == "comate" and isinstance(progress, dict):
                envelope["progress"] = progress
            outcome = self._deliver_approval_channel(run_id, request["approval_id"], channel, client, envelope, input_hash)
            if outcome.get("reason_code") == "APPROVAL_DELIVERY_CONFLICT":
                # The payload no longer matches the one this approval_id was claimed
                # for. Every channel is keyed on that same payload, so the rest would
                # conflict identically; the caller has to settle the input first.
                return {**(self.approvals.get(request["approval_id"]) or request), **outcome}
            outcomes.append({"channel": channel, **outcome})
        failed = [outcome for outcome in outcomes if outcome.get("reason_code") not in {None, "OK"}]
        if not failed:
            # A retry after a partial failure gets here; the mark has to go, or the row
            # keeps saying undeliverable while both channels are showing the card.
            if (self.approvals.get(request["approval_id"]) or {}).get("delivery_failed_at"):
                self.approvals.record_delivery_failure(request["approval_id"], None)
            return self.approvals.get(request["approval_id"]) or request

        # Aborting on the first failure meant one dead channel hid the request from
        # both audiences: Infoflow was never even attempted, so nobody saw the card
        # anywhere and the run looked stalled for no visible reason. Every channel is
        # attempted now, and a channel that already delivered short-circuits inside
        # `_deliver_approval_channel`, so a retry never posts a second card.
        #
        # A partial delivery is still a failure: `_reject` accepts a response only once
        # the receipts cover every channel, so a gate one channel can see cannot be
        # answered there either. What changes is that the operator gets told which
        # channel broke, and the reviewer at least sees the question.
        self.approvals.record_delivery_failure(request["approval_id"], {
            "reason_code": failed[0]["reason_code"],
            "channels": sorted(item["channel"] for item in failed),
            # The exception type, not its message: only key names are screened for
            # secrets, and a client's error text can quote the URL it was called with.
            # The full detail still reaches the operator in the returned result.
            "error_types": sorted({item["error_type"] for item in failed if item.get("error_type")}),
            "retry_allowed": all(item.get("retry_allowed") for item in failed),
        })
        return {
            **(self.approvals.get(request["approval_id"]) or request),
            **failed[0],
            "retry_allowed": all(item.get("retry_allowed") for item in failed),
            "channel_outcomes": outcomes,
        }

    def _deliver_approval_channel(
        self, run_id: str, approval_id: str, channel: str, client: Any, payload: dict[str, Any], payload_hash: str
    ) -> dict[str, Any]:
        # An abandoned delivery attempt is a closed, unknown outcome.  A later
        # explicit retry must chain a fresh idempotency key after that intent;
        # reusing the base key would find the abandoned receipt forever and
        # prevent the new approval card from ever being sent.
        key = self.state.live_idempotency_key(
            f"approval.delivery:{approval_id}:{channel}"
        )
        # Progress is a presentation snapshot, not part of the approval binding. It
        # can change between a retry while the approval id and input hash stay fixed;
        # hashing it here would turn a harmless refresh into an intent conflict.
        binding_payload = {key: value for key, value in payload.items() if key != "progress"}
        canonical_payload = json.dumps(binding_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical_payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        claim_payload = {"approval_id": approval_id, "canonical_payload_sha256": canonical_payload_hash}
        claim = self.state.claim_intent(run_id, f"approval.delivery.{channel}", key, claim_payload)
        if claim["status"] == "CONFLICT":
            return {"reason_code": "APPROVAL_DELIVERY_CONFLICT"}
        failure_key = f"{key}:failure"
        failed = self.state.idempotency_result(failure_key)
        if failed is not None:
            return failed
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return {"reason_code": "OK"}
        intent = claim["intent"]
        delivery_payload = {**payload, "_delivery_attempt_key": key}
        if claim["status"] == "EXISTING":
            reconciled = client.reconcile(delivery_payload) if hasattr(client, "reconcile") else None
            if reconciled is None:
                return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
            response = reconciled
        else:
            try:
                response = client.request(delivery_payload)
            except InfoflowRequestError as error:
                if error.outcome == "REJECTED_BEFORE_SEND":
                    return self._undelivered(run_id, channel, key, claim_payload, error)
                reconciled = client.reconcile(delivery_payload) if hasattr(client, "reconcile") else None
                if reconciled is None:
                    return {
                        "reason_code": "QUERY_REQUIRED",
                        "intent_id": intent["intent_id"],
                        "retry_allowed": False,
                        "error_type": type(error).__name__,
                        "outcome": error.outcome,
                        "detail": f"{type(error).__name__}: {error}",
                    }
                response = reconciled
            except ApprovalDeliveryBlocked as error:
                # Nothing was sent and nothing is unknown: report the policy reason
                # and leave no permanent failure, so the same card can be delivered
                # once the run is allowed to act again.
                return self._undelivered(run_id, channel, key, claim_payload, error)
            except ValueError as error:
                if channel == "infoflow" and str(error) == "APPROVAL_REQUEST_RESPONSE_INVALID":
                    failure = {"reason_code": "APPROVAL_REQUEST_RESPONSE_INVALID"}
                    return self.state.save_idempotency_result(failure_key, failure)
                reconciled = client.reconcile(delivery_payload) if hasattr(client, "reconcile") else None
                if reconciled is None:
                    return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
                response = reconciled
            except _NOTHING_SENT_EXCEPTIONS as error:
                return self._undelivered(run_id, channel, key, claim_payload, error)
            except Exception:
                reconciled = client.reconcile(delivery_payload) if hasattr(client, "reconcile") else None
                if reconciled is None:
                    return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
                response = reconciled
        if channel == "infoflow":
            gateway_result = parse_gateway_result(response)
            gateway_request = {**payload["approval"], "channel": channel}
            if (
                gateway_result is None
                or not is_clean_initial_pending_result(gateway_result)
                or not gateway_result_matches_request(gateway_result, gateway_request)
            ):
                failure = {"reason_code": "APPROVAL_REQUEST_RESPONSE_INVALID"}
                return self.state.save_idempotency_result(failure_key, failure)
            response = gateway_result
        self.state.receipt(intent["intent_id"], {"approval_id": approval_id, "channel": channel, "input_hash": payload_hash, "canonical_payload_sha256": canonical_payload_hash, "receipt": response}, [])
        self.approvals.record_delivery(approval_id, channel, response, payload_hash=payload_hash)
        return {"reason_code": "OK"}

    def _undelivered(
        self, run_id: str, channel: str, key: str, claim_payload: dict[str, Any], error: BaseException
    ) -> dict[str, Any]:
        """Close the claim for a card that provably never left this process.

        The claim exists so that an interrupted delivery is remembered as *maybe sent*.
        When the client raised before dispatching -- a missing method, a signature that
        does not match -- there is nothing to remember, and leaving the claim pending
        parks the whole run in `RECOVERY_REQUIRED` over a write nobody performed. The
        two alternatives are both wrong: a failure receipt would make
        `result_by_idempotency_key` report the card as delivered, and a saved failure
        result is permanent, so fixing the defect would still never let the card go out.

        `retry_allowed` is the distinction this pays for. `QUERY_REQUIRED` means the
        outcome is unknown and must be reconciled, never retried;
        `APPROVAL_DELIVERY_FAILED` means nothing was sent, so retrying after the fix is
        the correct move and cannot double-post. If the claim will not withdraw -- a
        receipt raced in, or the payload no longer matches -- then something did happen
        after all, and this falls back to the reconcile path.
        """
        withdrawn = self.state.withdraw_intent(run_id, f"approval.delivery.{channel}", key, claim_payload)
        outcome = {"error_type": type(error).__name__, "detail": f"{type(error).__name__}: {error}"}
        dispatch_outcome = getattr(error, "outcome", None)
        if isinstance(dispatch_outcome, str) and dispatch_outcome:
            outcome["outcome"] = dispatch_outcome
        if withdrawn.get("status") != "WITHDRAWN":
            return {
                **outcome, "reason_code": "QUERY_REQUIRED", "retry_allowed": False,
                "intent_id": (withdrawn.get("intent") or {}).get("intent_id"),
            }
        return {**outcome, "reason_code": "APPROVAL_DELIVERY_FAILED", "retry_allowed": True}

    def wait_infoflow_approval(self, run_id: str, approval_id: str, input_hash: str, infoflow_client: Any, timeout_seconds: float) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.get("run_id") != run_id or approval.get("run_id") == "legacy":
            return {"reason_code": "APPROVAL_RUN_MISMATCH", "run_id": run_id}
        # Guarded per approval instead of by decorator: reading the gate back is how
        # its decision lands, and a drifted run must still be able to answer the gate
        # that authorizes its own migration.
        if not migration_exempt(approval.get("action")):
            blocked = execution_guard(self.state, run_id)
            if blocked is not None:
                return blocked
        receipt = next((entry.get("receipt") for entry in approval["delivery_receipts"] if entry.get("channel") == "infoflow"), None)
        request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
        if not isinstance(request_id, str) or not request_id:
            return {"reason_code": "APPROVAL_DELIVERY_INCOMPLETE", "run_id": run_id}
        try:
            response = infoflow_client.wait(request_id, timeout_seconds)
        except (TypeError, ValueError):
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id
            )
        gateway_result = parse_gateway_result(response)
        if gateway_result is None or any(
            gateway_result.get(key) != value
            for key, value in {
                "request_id": request_id,
                "run_id": run_id,
                "approval_id": approval_id,
                "channel": "infoflow",
                "input_hash": input_hash,
            }.items()
        ) or gateway_result.get("member_policy") != approval.get("member_policy") or gateway_result.get(
            "deadline_at"
        ) != approval.get("deadline_at"):
            reply = response.get("reply") if isinstance(response, dict) else None
            responder = reply.get("responder") if isinstance(reply, dict) else None
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id, responder=responder
            )
        if gateway_result["reason_code"] is not None and gateway_result["status"] == "PENDING":
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id
            )
        if gateway_result["status"] == "TIMEOUT":
            return self.timeout_infoflow_approval(run_id, approval_id, input_hash)
        if gateway_result["status"] == "PENDING":
            return {
                "run_id": run_id,
                "approval_id": approval_id,
                "reason_code": "PENDING",
                "response": gateway_result,
            }
        reply = gateway_result["reply"]
        if reply is None:
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id
            )
        response_key = f"approval.gateway-reply:{approval_id}:{reply['reply_id']}"
        existing = self.state.idempotency_result(response_key)
        if existing is not None:
            return existing
        result = self.receive_infoflow_reply(
            run_id,
            approval_id,
            input_hash,
            {
                "run_id": gateway_result["run_id"],
                "approval_id": gateway_result["approval_id"],
                "channel": gateway_result["channel"],
                "input_hash": gateway_result["input_hash"],
                "decision": reply["decision"],
                "responder": reply["responder"],
            },
        )
        self.state.save_idempotency_result(response_key, result)
        return result

    def receive_infoflow_reply(self, run_id: str, approval_id: str, input_hash: str, response: Any) -> dict[str, Any]:
        # The migration gate is the one approval that must be answerable while
        # the run is drifted. A method-level guard cannot see the approval action,
        # so the old decorator rejected a verified migration reply before it
        # reached the ledger. Ordinary gates remain guarded per approval.
        approval = self.approvals.get(approval_id)
        if (
            isinstance(approval, dict)
            and approval.get("run_id") == run_id
            and not migration_exempt(approval.get("action"))
        ):
            blocked = execution_guard(self.state, run_id)
            if blocked is not None:
                return blocked
        if not isinstance(response, dict) or any(response.get(key) != value for key, value in {"run_id": run_id, "approval_id": approval_id, "input_hash": input_hash, "channel": "infoflow"}.items()):
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id,
                responder=response.get("responder") if isinstance(response, dict) else None,
            )
        decision = response.get("decision")
        if not isinstance(decision, str):
            return self.approvals.reject_envelope(
                approval_id, input_hash, "infoflow", run_id=run_id, responder=response.get("responder")
            )
        return self.approvals.receive(approval_id, decision, input_hash, "infoflow", response.get("responder"), run_id=run_id, state_store=self.state)

    @guard_execution
    def reissue_infoflow_approval(
        self,
        run_id: str,
        action: str,
        input_hash: str,
        *,
        member_policy: dict[str, list[str]],
        evidence: dict[str, Any] | None = None,
        infoflow_client: Any,
        comate_client: Any | None = None,
        allow_workflow_spec_migration: bool = False,
    ) -> dict[str, Any]:
        """Open a fresh attempt at a stuck gate, bound to the same input hash.

        A timed-out approval is terminal in the ledger and its row is unique per
        `(run_id, action, input_hash)`, so recovery cannot reuse it. The retry keeps
        the bound content and only takes a new action name, which leaves the expired
        attempt in the audit trail instead of overwriting it.

        A gate whose card reached nobody is stuck for the same reason and gets the same
        way out. It used to be refused as `APPROVAL_NOT_TIMED_OUT` and then waited for
        a deadline that no reviewer could ever answer, because a rejected gateway
        response is stored against the approval id permanently: the only way to make
        the request deliverable again is a new id. A delivery failure that says it is
        retryable is *not* stuck -- calling `request_infoflow_approval` again re-posts
        only the channel that failed -- so it is sent back down that path rather than
        being allowed to burn an action name.

        A REJECT is deliberately not reissuable. Re-asking a question a human answered,
        under the same input hash, is approval shopping; changing the input is what
        earns a new gate.
        """
        from approval_delivery import ComateApprovalClient

        attempts = [
            approval
            for approval in self.approvals.for_run(run_id)
            if approval["input_hash"] == input_hash
            and gate_of(approval["action"]) == action
        ]
        if not attempts:
            return {"run_id": run_id, "reason_code": "APPROVAL_NOT_FOUND"}
        latest = attempts[-1]
        from run_change import superseded

        if superseded(self.state, run_id, latest):
            # The run re-entered a phase after this gate was opened: its action is
            # gone, and asking it again would collect a decision nothing can use.
            return {
                "run_id": run_id,
                "approval_id": latest["approval_id"],
                "reason_code": "APPROVAL_SUPERSEDED",
            }
        undeliverable = latest.get("status") == "DELIVERY_FAILED"
        if latest.get("effective_decision") != "TIMEOUT" and not undeliverable:
            return {
                "run_id": run_id,
                "approval_id": latest["approval_id"],
                "reason_code": "APPROVAL_NOT_TIMED_OUT",
            }
        if undeliverable and (latest.get("delivery_failure") or {}).get("retry_allowed"):
            return {
                "run_id": run_id,
                "approval_id": latest["approval_id"],
                "reason_code": "APPROVAL_DELIVERY_RETRYABLE",
                "delivery_failure": latest.get("delivery_failure"),
            }
        retry = sum(1 for approval in attempts if _RETRY_MARKER in str(approval["action"])) + 1
        if retry > MAX_APPROVAL_RETRIES:
            return {
                "run_id": run_id,
                "approval_id": latest["approval_id"],
                "reason_code": "APPROVAL_RETRY_EXHAUSTED",
                "retries": retry - 1,
                "hint": _retry_exhausted_hint(self.state.events(run_id)),
            }
        return self.request_infoflow_approval(
            run_id,
            f"{action}#retry-{retry}",
            input_hash,
            member_policy=member_policy,
            evidence=evidence,
            comate_client=comate_client or ComateApprovalClient(),
            infoflow_client=infoflow_client,
            # A timed-out migration gate must be reissuable, otherwise the only path
            # out of drift disappears the moment nobody answers it in time.
            allow_workflow_spec_migration=migration_exempt(action),
        )

    @guard_execution
    def heartbeat_infoflow_approval(self, run_id: str, approval_id: str, observed_at: str) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.get("run_id") != run_id or approval.get("run_id") == "legacy":
            return {"run_id": run_id, "reason_code": "APPROVAL_RUN_MISMATCH"}
        return self.approvals.record_heartbeat(approval_id, observed_at)

    @guard_execution
    def timeout_infoflow_approval(self, run_id: str, approval_id: str, input_hash: str) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if (
            approval is not None
            and approval.get("run_id") == run_id
            and approval.get("input_hash") == input_hash
            and approval.get("effective_decision") == "TIMEOUT"
        ):
            handoff = {
                "handoff_id": f"approval-timeout-{approval_id}",
                "status": "PENDING",
            }
            self.state.record_handoff(
                run_id,
                handoff["handoff_id"],
                {
                    "approval_id": approval_id,
                    "input_hash": input_hash,
                    "reason_code": "APPROVAL_TIMEOUT",
                },
            )
            return {**approval, "reason_code": "APPROVAL_TIMEOUT", "handoff": handoff}
        return self.approvals.timeout(approval_id, input_hash, run_id=run_id, state_store=self.state)


def _collaboration_binding(
    run_id: str,
    requirement_id: str,
    project: str,
    profile_hash: str,
    profile: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    card_id = snapshot.get("canonical_card_id")
    title = snapshot.get("title")
    content_hash = snapshot.get("content_hash")
    channels = profile.get("approval_channels") if isinstance(profile, dict) else None
    members = channels.get("role_members") if isinstance(channels, dict) else None
    if (
        card_id != requirement_id
        or not isinstance(title, str)
        or not title
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(character not in "0123456789abcdef" for character in content_hash.lower())
        or not isinstance(members, dict)
    ):
        return {"reason_code": "MEMBER_CONFIRMATION_REQUIRED", "unresolved": []}
    return build_collaboration_binding(
        run_id, project, card_id, profile_hash, title, content_hash, members,
        channels.get("group_topic"),
    )


def _ku_acceptance_candidates(project: str, config_root: Any = None) -> dict[str, Any]:
    """Read acceptance criteria out of the configured KU requirement doc as candidates.

    Advisory only. The snapshot is never rewritten, so these must be confirmed with the
    requirement owner and recorded by `tom-grill` in `acceptance_delta`; `source` belongs
    in each delta entry's evidence. Deliberately not part of any phase action identity: a
    live document fetch must not make `action_id` or an approval `input_hash` unstable.
    """
    from clients.ku_client import KuClient, resolve_username
    from project_registry import load_profile, profile_path

    loaded = load_profile(profile_path(project, config_root))
    if not loaded.get("ready"):
        return {"ok": False, "reason_code": "PROJECT_NOT_READY", "project": project}
    ku_sources = sorted(
        (
            item
            for item in loaded["profile"].get("knowledge_sources", [])
            if isinstance(item, dict) and item.get("provider") == "ku"
        ),
        key=lambda item: item.get("priority", 0),
    )
    if not ku_sources:
        return {"ok": False, "reason_code": "KU_SOURCE_NOT_CONFIGURED", "project": project}
    repo_paths = [
        repo["path"]
        for repo in loaded["profile"].get("business_repos", [])
        if isinstance(repo, dict) and repo.get("path")
    ]
    username = resolve_username(repo_paths)
    if not username:
        # Without an identity the CLI answers every query with `开放应用不存在`, which
        # would otherwise look like a missing document.
        return {"ok": False, "reason_code": "KU_USERNAME_REQUIRED", "project": project}
    source = ku_sources[0]
    found = KuClient(repo_id=source["repo_id"], username=username).requirement_acceptance(
        source["parent_doc_id"]
    )
    return {**found, "project": project, "confirmation_required": True}


def _runtime_failure_category(reason_code: str, classification: Any = None) -> str:
    normalized = str(classification or reason_code).upper()
    if any(word in normalized for word in ("AUTH", "LOGIN")):
        return "auth"
    if "PERMISSION" in normalized:
        return "platform"
    if "RELEASE" in normalized:
        return "release-rule"
    if any(word in normalized for word in ("PLATFORM", "TRANSIENT", "TIMEOUT", "QUERY")):
        return "platform"
    if "MIXED" in normalized:
        return "mixed"
    if any(word in normalized for word in ("ENV", "RUNNER", "CAPACITY")):
        return "environment"
    if any(word in normalized for word in ("TEST", "REGRESSION", "INTEGRATION")):
        return "test-case"
    if "INTERFACE" in normalized:
        return "interface"
    if any(word in normalized for word in ("CODE", "REVISION", "COMPILE", "BUILD", "CR_", "SUBMIT_")):
        return "code"
    return "mixed"


def _roles_for_category(category: str) -> list[str]:
    if category in {"environment", "test-data", "test-case", "regression", "integration"}:
        return ["test"]
    if category in {"code", "interface", "spec", "task-plan", "review"}:
        return ["development"]
    if category in {"auth", "platform", "release-rule"}:
        return ["project"]
    return ["development", "test"]


def _stale_checkpoint_plan(events: list[dict[str, Any]], task_id: str) -> str | None:
    """Find the exact Plan pin carried by the stale task's latest IMPLEMENT event."""
    for event in reversed(events):
        if event.get("state") != "IMPLEMENT":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("task_id") == task_id and isinstance(payload.get("plan_artifact_id"), str):
            return payload["plan_artifact_id"]
    return None


def _valid_source_revisions(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and {"business", "tests"}.issubset(value)
        and all(isinstance(revision, str) and revision for revision in value.values())
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_path(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not left or not isinstance(right, str) or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return False


def _is_runtime_failure(reason_code: str) -> bool:
    normalized = reason_code.upper()
    if normalized.startswith(
        ("CR_", "SUBMIT_", "TRIGGER_", "RERUN_", "MONITOR_", "BUILD_", "RELEASE_", "ICODE_", "IPIPE_", "PIPELINE_")
    ):
        return True
    return any(
        word in normalized
        for word in (
            "ICODE", "IPIPE", "PIPELINE", "REVISION", "INTERFACE", "TEST_FAILURE",
            "ENVIRONMENT_FAILURE", "MIXED_FAILURE", "AUTH_REQUIRED", "PERMISSION_DENIED",
            "RELEASE_RULE", "COMPILE_FAILURE", "CODE_FAILURE",
        )
    )


def _reviewed_tasks(orchestrator: Any, run_id: str) -> list[str]:
    """Tasks whose Review passed, i.e. the change sets this run owes iCode."""
    from phase_protocol import _passing_review

    found: dict[str, None] = {}
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        if artifact.get("kind") != "change-set":
            continue
        metadata = artifact.get("metadata") or {}
        task_id = metadata.get("task_id")
        if metadata.get("verdict") == "PASS" and isinstance(task_id, str) and task_id:
            found.setdefault(task_id, None)
    if found:
        return sorted(found)
    # No descriptor archived yet: fall back to the reviews themselves, so a run that
    # predates the descriptor step is not mistaken for having nothing to submit.
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        envelope = artifact.get("envelope")
        if not isinstance(envelope, dict) or envelope.get("phase") != "REVIEW":
            continue
        task_id = envelope.get("task_id")
        if isinstance(task_id, str) and task_id and _passing_review(envelope.get("content") or {}):
            found.setdefault(task_id, None)
    return sorted(found)


def _recorded_submissions(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    from pipeline_plan import current_submissions
    return current_submissions(orchestrator, run_id)


def _outstanding_submissions(orchestrator: Any, run_id: str) -> list[str]:
    """Reviewed tasks whose current change set has not reached iCode yet.

    A submission records the change set it carried, and the descriptor records which
    task that change set belongs to, so the two are joined through `change_set_id`.
    A run with no descriptors at all cannot be judged this way — it predates the
    descriptor step — and is left with the original behaviour of transitioning on its
    first submission rather than being deadlocked by a rule it cannot satisfy.

    The join is against the task's *current* change set, not any change set it ever
    passed a Review with. A repair out of SUBMIT gives a task a second passing change
    set, and matching on "some change set of this task was submitted" would let the
    first one's receipt answer for the second: the run would transition to IPIPE on a
    sibling task's submission while the repaired code sat in the worktree, and the
    pipelines would build the defect the repair existed to remove.

    An amendment republishes the DAG. A Review older than that DAG does not close
    the new scope, so a sibling's historical PASS descriptor cannot answer IPIPE
    either: BGW-1956 T0's empty submit otherwise jumped while T1/T2 predated the
    DAG and T3's latest Review was REJECT.
    """
    protocol = orchestrator.phase_protocol()
    dag = orchestrator.artifacts.latest_phase(run_id, "TASKS", None)
    dag_nodes: list[str] = []
    if dag.get("valid"):
        for node in dag["envelope"].get("content", {}).get("nodes") or []:
            if isinstance(node, dict) and isinstance(node.get("task_id"), str) and node["task_id"]:
                dag_nodes.append(node["task_id"])
    current_passing = protocol._current_passing(run_id) if dag_nodes else set()
    from pipeline_plan import current_descriptors
    descriptors = current_descriptors(orchestrator, run_id)
    submitted_tasks = _submitted_tasks(orchestrator, run_id, descriptors)
    if dag_nodes:
        return sorted(set(dag_nodes) - (current_passing & submitted_tasks))
    return sorted(set(descriptors) - submitted_tasks)


def _submitted_tasks(orchestrator: Any, run_id: str,
                     descriptors: dict[str, Any] | None = None) -> set[str]:
    from pipeline_plan import current_descriptors
    if descriptors is None:
        descriptors = current_descriptors(orchestrator, run_id)
    submitted = {(item.get("change_set_id"), item.get("revision_set_id"))
                 for item in _recorded_submissions(orchestrator, run_id)}
    return {task for task, entry in descriptors.items()
            if (entry["descriptor"].get("change_set_id"),
                entry["descriptor"].get("revision_set_id")) in submitted}


def _current_pass_change_sets(orchestrator: Any, run_id: str) -> dict[str, str]:
    """Task -> current descriptor ID; submission completion also checks its revision."""
    from pipeline_plan import current_descriptors
    return {task: str(entry["descriptor"]["change_set_id"])
            for task, entry in current_descriptors(orchestrator, run_id).items()}


def _latest_unsubmitted_reviewed_task(orchestrator: Any, run_id: str) -> str | None:
    """The newest DAG-covering PASS whose exact revision is not in iCode."""
    unsubmitted = set(_current_pass_change_sets(orchestrator, run_id)) - _submitted_tasks(orchestrator, run_id)
    if not unsubmitted:
        return None
    reviews = orchestrator.phase_protocol()._passing_reviews(run_id)
    return max(unsubmitted, key=lambda task: (reviews.get(task, 0), task))


def _submit_followup_state(orchestrator: Any, run_id: str) -> str:
    outstanding = _outstanding_submissions(orchestrator, run_id)
    if not outstanding:
        return "IPIPE"
    current = set(_current_pass_change_sets(orchestrator, run_id))
    if (set(outstanding) & current) - _submitted_tasks(orchestrator, run_id):
        return "SUBMIT"
    return "WORKSPACE"


def _stage_parameters(
    orchestrator: Any, runtime: Any, run_id: str, names: list[str],
    stage_build_id: str | None = None, *, record_evidence: bool = False,
) -> dict[str, Any]:
    """Derive the values a manual stage would otherwise ask a person to type.

    The CR id is the submission this run made; the product download is the compile job's
    published URL plus the per-repository irepo token. Both follow each CR's current
    patchset, so the product a stage downloads is the one the pipeline is actually
    building. Nothing is guessed: an input that cannot be derived is reported by name.

    Read-only by default. `record_evidence=True` (G8 material preparation or execution
    only) appends each product-download outcome as stage-bound dependency evidence.
    """
    from stage_parameters import load_tokens, resolve, project_mapping

    declaration = None
    if stage_build_id is not None:
        declaration = runtime.stage_parameter_names(stage_build_id)
        if not declaration.get("ok"):
            return declaration
        if names and set(names) != set(declaration["names"]):
            return {"ok": False, "reason_code": "STAGE_PARAMETER_NAMES_MISMATCH",
                    "missing_parameters": sorted(set(declaration["names"]) - set(names)),
                    "declared_parameters": declaration["names"]}
        names = declaration["names"]
    if not names:
        if declaration is None:
            return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
        return {"ok": True, "reason_code": "OK", "parameters": {}, "declaration": declaration}

    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    mapping = project_mapping(profile, declaration["stage_name"]) if declaration else None
    if mapping is not None and any(name not in mapping for name in names):
        return {"ok": False, "reason_code": "STAGE_PARAMETER_UNRESOLVED",
                "missing_parameters": [
                    {"parameter": name, "detail": "PARAMETER_MAPPING_REQUIRED"}
                    for name in names if name not in mapping
                ], "declaration": declaration}
    derived = _ipipe_revision_set(orchestrator, run_id, profile)
    if not derived.get("ok"):
        return derived
    revisions = {
        str(item["module"]): str(item["revision"])
        for item in derived["revisions"]["repositories"]
    }
    change_number = None
    test_module = (profile.get("test_repo") or {}).get("module")
    candidates = set()
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        if artifact.get("kind") != "submission":
            continue
        try:
            submitted = json.loads(artifact["content"].decode("utf-8"))
        except (AttributeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (submitted.get("module") == test_module
                and str(submitted.get("revision")) == revisions.get(test_module)
                and submitted.get("change_number")):
            candidates.add(str(submitted["change_number"]))
    if len(candidates) == 1:
        change_number = next(iter(candidates))
    # A CR-valued parameter requires an exact module/revision receipt. An unrelated
    # single open CR or the newest receipt cannot authorize this run's stage input.
    product_urls: dict[str, str] = {}
    required_modules = {mapping[name] for name in names} if mapping is not None else set(revisions)
    from pipeline_plan import frozen_plan
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is None:
        return {"ok": False, "reason_code": "FROZEN_PRODUCT_PLAN_REQUIRED"}
    products = {}
    for module, revision in revisions.items():
        if module not in required_modules:
            continue
        found = runtime.product_for_plan(plan, module)
        if record_evidence and stage_build_id is not None and declaration is not None:
            # B-21: only a caller about to write G8 material or execute records; each
            # attempt is bound to this stage and to the declared parameters that consume
            # the module, and the newest attempt wins (environment_evidence).
            from environment_evidence import record_dependency_attempt
            owned = runtime._load_stage_binding(stage_build_id) or {}
            try:
                record_dependency_attempt(
                    orchestrator.state, run_id, build_id=owned.get("build_id"),
                    stage_build_id=stage_build_id,
                    stage_conf_id=(owned.get("stage") or {}).get("stage_conf_id"),
                    module=module,
                    consumed_by=[name for name in names if (mapping or {}).get(name) == module],
                    declared_parameters=names, result=found,
                    source="stage-parameter product download",
                )
            except Exception:  # noqa: BLE001 - evidence only; the result is still returned
                logging.getLogger(__name__).warning(
                    "dependency evidence for %s not recorded", module, exc_info=True)
        if not found.get("ok"):
            return found
        products[module] = found
        # A known module with no published product keeps an empty entry, so the failure
        # names the module and revision instead of "not derivable".
        product_urls[module] = found["product_url"] if found.get("ok") else ""
    try:
        tokens = load_tokens(orchestrator.config_root)
    except ValueError as error:
        return {"ok": False, "reason_code": str(error), "run_id": run_id}
    result = resolve(
        list(names), change_number=change_number, product_urls=product_urls, tokens=tokens,
        mapping=mapping,
    )
    return {**result, "declaration": declaration, "mapping": mapping,
            "product_revisions": {module: products[module]["revision"] for module in product_urls},
            "products": products, "pipeline_plan_hash": plan["plan_hash"]}


@guard_execution
def _prepare_ipipe_g8(
    orchestrator: Any, runtime: Any, run_id: str, stage_build_id: str,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Persist only the exact redacted candidate; never settle or execute the gate."""
    from stage_parameters import redacted

    hashed = runtime.rerun_input_hash(stage_build_id, parameters=resolved["parameters"])
    if not hashed.get("ok"):
        return hashed
    from persistence_policy import redact_secrets

    # Approval material is shown on the card and stored; credentials never reach it.
    material = redact_secrets({
        "binding": hashed["binding"], "declaration": resolved["declaration"],
        "parameters": redacted(resolved["parameters"]), "mapping": resolved.get("mapping"),
        "product_revisions": resolved.get("product_revisions", {}),
        "products": resolved.get("products", {}),
        "pipeline_plan_hash": resolved.get("pipeline_plan_hash"),
        "case_failures": hashed.get("case_failures", []),
    })
    orchestrator.state.save_idempotency_result(
        f"ipipe.g8-material:{run_id}:{hashed['input_hash']}", material
    )
    return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id,
            "gate": "G8", "approval_input_hash": hashed["input_hash"], "controller": "ipipe",
            "module": hashed["binding"]["module"], "stage_build_id": stage_build_id,
            "material": material}


def _open_repo_reviews(module: Any, path: Any) -> list[dict[str, Any]] | None:
    """The open CRs of one repository, or None when iCode cannot be asked."""
    from cli_transport import ProcessTransport

    if not module or not path:
        return None
    for candidate in ("/Users/tom/.icode/bin/icode-cli", "icode-cli"):
        try:
            result = ProcessTransport().run(
                [candidate, "api", "get_repo_reviews", "--repo", str(module), "--status", "NEW", "-o", "json"],
                cwd=str(path), timeout=60,
            )
        except Exception:
            continue
        if result["returncode"] != 0:
            continue
        try:
            changes = json.loads(result["stdout"]).get("data", {}).get("changes", [])
        except (AttributeError, json.JSONDecodeError):
            return None
        return [change for change in changes if isinstance(change, dict)]
    return None


def _open_test_repo_cr(profile: dict[str, Any], run_id: str) -> str | None:
    """The requirement's still-open CR in the test repository, if exactly one is open."""
    repository = profile.get("test_repo") or {}
    changes = _open_repo_reviews(repository.get("module"), repository.get("path"))
    if changes is None:
        return None
    numbers = [str(change.get("_number")) for change in changes if change.get("_number")]
    return numbers[0] if len(numbers) == 1 else None


def _current_patchset(module: Any, path: Any, change_number: Any) -> str | None:
    """The revision iCode currently serves for one still-open CR.

    A submission receipt records the revision that was pushed at the time. Every later
    patchset -- a repair, a fold of a sibling CR -- moves the CR forward without writing a
    new receipt, so the receipt alone cannot say what the pipeline is building.
    """
    changes = _open_repo_reviews(module, path)
    if changes is None or not change_number:
        return None
    for change in changes:
        if str(change.get("_number")) == str(change_number):
            revision = change.get("current_revision")
            return str(revision) if revision else None
    return None


def _ipipe_revision_set(
    orchestrator: Any, run_id: str, profile: dict[str, Any]
) -> dict[str, Any]:
    """The repositories and revisions the pipeline is building for this run.

    Build ownership has to be anchored to something the run can prove is its own: the CR
    number comes from a submission receipt, and the revision comes from that CR's current
    patchset. A repair or a fold that adds a patchset therefore stays ownable, while a
    build of somebody else's change still fails to match.
    """
    from pipeline_plan import frozen_plan
    plan = frozen_plan(orchestrator.state.events(run_id))
    if plan is not None:
        return {"ok": True, "reason_code": "OK", "drift": {},
                "revisions": plan["revision_set"]}
    business = [item for item in (profile.get("business_repos") or []) if isinstance(item, dict)]
    test_repo = profile.get("test_repo") or {}
    if not business or not test_repo:
        return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
    pinned: dict[str, str] = {}
    change_numbers: dict[str, str] = {}
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        if artifact.get("kind") != "submission":
            continue
        try:
            submitted = json.loads(artifact["content"].decode("utf-8"))
        except (AttributeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for entry in (submitted.get("revision_set") or {}).values():
            if isinstance(entry, dict) and entry.get("module") and entry.get("revision"):
                pinned[str(entry["module"])] = str(entry["revision"])
        if submitted.get("module") and submitted.get("change_number"):
            change_numbers[str(submitted["module"])] = str(submitted["change_number"])
    # The newest receipt can name a CR that was folded away; the open list is authoritative.
    open_test = _open_test_repo_cr(profile, run_id)
    if open_test:
        change_numbers[str(test_repo.get("module"))] = open_test
    repositories: list[dict[str, Any]] = []
    drift: dict[str, Any] = {}
    for kind, repository in [("business", item) for item in business] + [("test", test_repo)]:
        module = str(repository.get("module") or "")
        revision = pinned.get(module)
        current = _current_patchset(module, repository.get("path"), change_numbers.get(module))
        if current and current != revision:
            drift[module] = {"submitted": revision, "current": current}
            revision = current
        if not revision:
            return {"ok": False, "reason_code": "REVISION_UNRESOLVED", "run_id": run_id,
                    "module": module}
        repositories.append({
            "kind": kind, "module": module,
            "branch": repository.get("branch"), "revision": revision,
        })
    revision_set_id = hashlib.sha256(
        json.dumps(repositories, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True, "reason_code": "OK", "drift": drift,
        "revisions": {
            "run_id": run_id,
            "revision_set_id": revision_set_id,
            "repositories": repositories,
            "parameters": {},
        },
    }


def _adoption_result_key(target_run_id: str, diagnosis_artifact_id: str, plan_artifact_id: str) -> str:
    return f"adopt-code-only-repair:{target_run_id}:{diagnosis_artifact_id}:{plan_artifact_id}"


def adoption_transition_decision(payload: dict[str, Any], g6_approval: dict[str, Any]) -> dict[str, Any]:
    """The controlled INTAKE -> IMPLEMENT decision for `adopt-code-only-repair`.

    Not a workflow-spec edge (that would change the spec hash): allowed only with the
    adoption's own evidence -- an approved G6 for the adopted diagnosis, a pinned Plan
    and both baselines -- and recorded as the event's policy decision.
    """
    from transition_policy import POLICY_ID

    revisions = payload.get("source_revisions")
    complete = (
        payload.get("adoption_kind") == "CODE_ONLY_REPAIR"
        and isinstance(payload.get("plan_artifact_id"), str)
        and isinstance(payload.get("plan_content_hash"), str)
        and isinstance(revisions, dict) and {"business", "tests"} <= set(revisions)
        and isinstance(g6_approval, dict)
        and g6_approval.get("action") == "G6"
        and g6_approval.get("effective_decision") == "APPROVE"
    )
    if not complete:
        return {"allowed": False, "reason_code": "ADOPTION_EVIDENCE_MISSING",
                "current_state": "INTAKE", "next_state": "IMPLEMENT", "policy_id": POLICY_ID}
    return {"allowed": True, "reason_code": "OK", "current_state": "INTAKE",
            "next_state": "IMPLEMENT", "policy_id": POLICY_ID,
            "controlled": "CODE_ONLY_ADOPTION",
            "evidence": {"g6_approval_id": g6_approval.get("approval_id"),
                         "diagnosis_artifact_id": payload.get("source_diagnosis_artifact_id"),
                         "plan_artifact_id": payload.get("plan_artifact_id")}}


def _adoption_budget(protocol: Any, state: Any, source_run_id: str, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Whether the source run's repair budget still allows the adopted repair round.

    Uses the same history and policy as an in-run DIAGNOSE completion
    (`PhaseProtocol._repair_history` + `repair_policy.next_action`), so the decision
    matches what the source run itself would decide for this diagnosis.
    """
    import repair_policy

    history = protocol._repair_history(source_run_id, diagnosis.get("envelope") or {})
    rounds = sum(1 for entry in history if entry.get("diagnosis_confirmed"))
    verdict = repair_policy.next_action(history)
    if verdict.get("action") == "REPAIR":
        return {"ok": True, "rounds": rounds}
    reason = verdict.get("reason_code")
    if reason in {"NO_PROGRESS", "THREE_FAILED_FIXES"}:
        events = state.events(source_run_id)
        diagnose_source = (diagnosis.get("envelope") or {}).get("source_event_id")
        positions = [index for index, event in enumerate(events) if event.get("event_id") == diagnose_source]
        after = positions[0] if positions else len(events)
        if any(event.get("state") == "ARCHITECTURE_REVIEW" for event in events[after + 1:]):
            return {"ok": True, "rounds": rounds}
    return {"ok": False, "reason_code": "ADOPTION_BUDGET_EXHAUSTED", "repair_rounds": rounds,
            "budget_reason": reason, "repair_round_limit": repair_policy.REPAIR_ROUND_LIMIT}


def _under_run_lease(orchestrator: Any, run_id: str, call: Any) -> dict[str, Any]:
    """Run one CLI write under the same per-run lease the worker drives the run with.

    `ipipe-rerun` reaches the platform directly from the CLI. Without the lease a CLI
    rerun and a worker (or a second CLI) could interleave on one run; the atomic claim in
    the state store already prevents two POSTs per stage, and the lease serializes the
    rest of the write path (stage binding, receipts, approval settlement) the same way
    `worker_driver.submit_draft` / `report_producer_block` do.
    """
    import uuid as _uuid
    from worker_driver import _WORKER_LEASE_PREFIX, _WORKER_LEASE_TTL_SECONDS

    locks = getattr(getattr(orchestrator, "recovery", None), "locks", None)
    if locks is None:
        return call()
    token = _uuid.uuid4().hex
    key = f"{_WORKER_LEASE_PREFIX}{run_id}"
    lease = locks.acquire(key, token, _WORKER_LEASE_TTL_SECONDS)
    if not lease.get("acquired"):
        return {"ok": False, "reason_code": "WORKER_LEASE_HELD", "run_id": run_id,
                "owner_pid": lease.get("owner_pid"), "retry_allowed": True}
    try:
        return call()
    finally:
        released = locks.release(key, token)
        if isinstance(released, dict) and not released.get("released"):
            logging.getLogger(__name__).warning("run lease %s not released by ipipe-rerun", key)


@guard_execution
def _ipipe_adopt(
    orchestrator: Any, runtime: Any, run_id: str, module: Any, window_seconds: float
) -> dict[str, Any]:
    """Bind the build the platform already ran for this run's CRs, and bind its stages.

    Reads only: `discover` selects the build whose repositories and revisions match, and
    one `monitor` pass records the stage ownership that a later G8 re-run checks. Without
    this step a build triggered from the iPipe page is invisible to the run, and every
    stage operation refuses with `STAGE_OWNERSHIP_UNVERIFIED`.
    """
    from datetime import datetime, timedelta, timezone

    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    derived = _ipipe_revision_set(orchestrator, run_id, profile)
    if not derived.get("ok"):
        return derived
    found = runtime.discover(profile, derived["revisions"], module)
    if not found.get("ok"):
        return {**found, "drift": derived["drift"], "run_id": run_id}
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=window_seconds)).isoformat()
    observed = runtime.monitor(found["build_id"], deadline)
    return {
        "ok": True, "reason_code": "OK", "run_id": run_id,
        "build_id": found["build_id"], "drift": derived["drift"],
        "monitor": {key: observed.get(key) for key in ("ok", "reason_code", "status")},
        "stages": [
            {"stage_build_id": stage.get("stage_build_id"), "name": stage.get("name"),
             "status": stage.get("status")}
            for stage in observed.get("stages") or [] if isinstance(stage, dict)
        ],
    }


def _live_submit_key(state: Any, base_key: str) -> str:
    """The key the current submission attempt lives under.

    Abandoning an attempt leaves its intent and receipt in place and chains the next
    attempt off it, so the newest attempt is the end of that chain.
    """
    key = base_key
    for _ in range(8):
        stored = state.result_by_idempotency_key(key)
        response = stored["receipt"]["response"] if isinstance(stored, dict) else None
        if not (isinstance(response, dict) and response.get("abandoned") is True):
            return key
        prior = state.intent_by_idempotency_key(key)
        if not isinstance(prior, dict):
            return key
        key = f"{base_key}:after:{prior['intent_id']}"
    return key


def _ipipe_entry_payload(
    intake: dict[str, Any],
    plan: dict[str, Any],
    primary_target: dict[str, Any],
    approval_id: Any,
    input_hash: Any,
    *,
    previous_state: str = "SUBMIT",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The IPIPE-entry payload, shared by SUBMIT->IPIPE and the legacy-plan migration.

    Keeping one builder guarantees every reader that scans the latest IPIPE event
    (frozen_plan, the single-pipeline binding checks, the IPIPE controller binding)
    sees the same shape whether the plan was frozen at submit time or backfilled for
    an in-flight run created before pipeline plans existed.
    """
    payload = {
        "previous_state": previous_state,
        "requirement_id": intake["requirement_id"],
        "project": intake["project"],
        "profile_path": intake["profile_path"],
        "profile_hash": intake["profile_hash"],
        # The single-pipeline IPIPE checks still read these, so they name the
        # primary business repository rather than whichever submission happened to
        # be last; `submissions` carries the rest for the per-module checks.
        **{key: primary_target[key] for key in (
            "pipeline_id", "module", "release_rule", "source_revisions", "environment_fingerprint")},
        "submission_artifact_id": primary_target["artifact_id"],
        "submission_hash": primary_target["sha256"],
        "submissions": plan["submissions"],
        "pipeline_plan": plan,
        "approval_id": approval_id,
        "approval_input_hash": input_hash,
    }
    if extra:
        payload.update(extra)
    return payload


def _primary_submission(
    profile: dict[str, Any],
    submissions: list[dict[str, Any]],
    controller_binding: dict[str, Any],
    submission: dict[str, Any],
) -> dict[str, Any]:
    """The submission the legacy single-pipeline payload should describe.

    `business_repos[0]` is what `ipipe_runtime._context` and the IPIPE binding checks
    already treat as the run's module, so naming it here keeps those checks meaningful
    instead of pointing them at whichever repository was submitted last.
    """
    repos = profile.get("business_repos") or []
    primary_module = repos[0].get("module") if repos and isinstance(repos[0], dict) else None
    for item in reversed(submissions):
        if item["controller_binding"].get("module") == primary_module:
            return item
    return {
        "artifact_id": submission["artifact_id"],
        "sha256": submission["sha256"],
        "controller_binding": controller_binding,
    }


def _submission_controller_binding(
    profile: Any, receipt: Any, change_set: Any
) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(profile, dict) or not isinstance(receipt, dict) or not isinstance(change_set, dict):
        return "PROJECT_NOT_READY", {}
    pipeline = profile.get("pipeline_profile")
    repositories = profile.get("business_repos")
    test_repository = profile.get("test_repo")
    revision_set = receipt.get("revision_set")
    if (
        not isinstance(pipeline, dict)
        or not isinstance(repositories, list)
        or not isinstance(test_repository, dict)
        or not isinstance(revision_set, dict)
        or set(revision_set) != {"business", "test"}
    ):
        return "SOURCE_REVISION_REQUIRED", {}
    business = revision_set.get("business")
    tests = revision_set.get("test")
    if not isinstance(business, dict) or not isinstance(tests, dict):
        return "SOURCE_REVISION_REQUIRED", {}
    # A task that only touches the test repository submits that repository, so the
    # receipt's module and revision belong to the test entry. Checking them against the
    # business entry regardless refused those submissions with SOURCE_REVISION_MISMATCH.
    # The role is derived from which entry the receipt matches, so the canonical
    # descriptor bytes stay unchanged.
    primary = next((
        entry for entry in (business, tests)
        if entry.get("module") == change_set.get("module")
        and entry.get("revision") == change_set.get("commit_revision")
    ), None)
    if primary is None:
        return "SOURCE_REVISION_MISMATCH", {}
    owned_business = next((
        repository for repository in repositories
        if isinstance(repository, dict) and repository.get("module") == business.get("module")
    ), None)
    if (
        not isinstance(owned_business, dict)
        or business.get("branch") != owned_business.get("branch")
        or tests.get("module") != test_repository.get("module")
        or tests.get("branch") != test_repository.get("branch")
        or receipt.get("module") != change_set.get("module")
        or receipt.get("module") != primary.get("module")
        or receipt.get("commit_revision") != primary.get("revision")
        or receipt.get("patchset") != primary.get("revision")
        or receipt.get("revision_set") != change_set.get("revision_set")
    ):
        return "SOURCE_REVISION_MISMATCH", {}
    source_revisions = {
        "business": business.get("revision"),
        "tests": tests.get("revision"),
    }
    if not all(isinstance(value, str) and value for value in source_revisions.values()):
        return "SOURCE_REVISION_REQUIRED", {}
    # One pipeline per module: the binding has to pin the pipeline that will actually
    # gate *this* module, or IPIPE evidence for it fails `SUBMISSION_BINDING_MISMATCH`.
    from phase_protocol import _registered_pipeline

    pipeline_id = _registered_pipeline(pipeline, receipt.get("module"))
    entries = pipeline.get("pipelines") or []
    module_entry = next(
        (entry for entry in entries if isinstance(entry, dict) and entry.get("module") == receipt.get("module")),
        None,
    )
    release_rule = (
        module_entry.get("release_rule", pipeline.get("release_rule"))
        if isinstance(module_entry, dict)
        else pipeline.get("release_rule")
    )
    if not all(isinstance(value, str) and value for value in (pipeline_id, release_rule)):
        return "PROJECT_NOT_READY", {}
    environment = profile.get("environment_profile")
    return None, {
        "pipeline_id": pipeline_id,
        "module": receipt["module"],
        "release_rule": release_rule,
        "source_revisions": source_revisions,
        "environment_fingerprint": hashlib.sha256(json.dumps(
            environment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="tom-autodev")
    parser.add_argument("--config-root", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("requirement_id")
    start.add_argument("project")
    start.add_argument(
        "--fresh",
        action="store_true",
        help="在已停止的同卡片 run 之外创建新的审计 run；不会删除旧事件或产物",
    )

    status = subparsers.add_parser("status")
    status.add_argument("run_id")
    status.add_argument(
        "--json", action="store_true", help="输出完整事件流 JSON，默认输出人可读的进度摘要"
    )

    context = subparsers.add_parser("context", help="读取当前 run 的固定任务上下文，不执行阶段")
    context.add_argument("target", help="run id 或唯一活动的 iCafe 卡片 id")

    process = subparsers.add_parser(
        "process", help="创建或定位需求 run，并驱动到下一个需要处理的位置"
    )
    process.add_argument("requirement_id")
    process.add_argument("project")
    process.add_argument("--icode-skill", default=None)

    approve = subparsers.add_parser("approve")
    approve.add_argument("approval_id")
    approve.add_argument("decision")
    approve.add_argument("input_hash")
    approve.add_argument("channel")
    approve.add_argument("run_id")
    approve.add_argument("responder")

    resume = subparsers.add_parser("resume")
    resume.add_argument("run_id")

    drive = subparsers.add_parser("drive", help="由 WorkerDriver 驱动到下一个需要处理的位置")
    drive.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")
    drive.add_argument("--icode-skill", default=None)

    continue_run = subparsers.add_parser("continue", help="消费有效 handoff 或从检查点继续驱动")
    continue_run.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")
    continue_run.add_argument("--icode-skill", default=None)

    submit_draft = subparsers.add_parser("submit-draft", help="提交当前 ProducerJob 的 DraftContent JSON 文件")
    submit_draft.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")
    submit_draft.add_argument("job_id")
    submit_draft.add_argument("draft_json")
    prepare_change = subparsers.add_parser(
        "prepare-change-set", help="只读提取当前 IMPLEMENT 自有干净 HEAD 的完整 diff 并返回 JSON"
    )
    prepare_change.add_argument("target")
    prepare_change.add_argument("job_id")
    prepare_change.add_argument("metadata_json")
    prepare_change.add_argument("--output", help="将 content 独占创建为新 JSON 文件；已存在则拒绝覆盖")
    block_producer = subparsers.add_parser(
        "report-producer-block", help="报告 ProducerJob 无法真实产出并保留固定绑定证据"
    )
    block_producer.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")
    block_producer.add_argument("job_id")
    block_producer.add_argument("report_json")
    conflict_producer = subparsers.add_parser(
        "report-producer-conflict", help="报告 ProducerJob 与前序行为契约冲突"
    )
    conflict_producer.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")
    conflict_producer.add_argument("job_id")
    conflict_producer.add_argument("report_json")

    stop = subparsers.add_parser("stop")
    stop.add_argument("target", help="run id 或唯一的 iCafe 卡片 id")

    next_action = subparsers.add_parser("next")
    next_action.add_argument("run_id")

    complete = subparsers.add_parser("complete-phase")
    complete.add_argument("run_id")
    complete.add_argument("envelope")

    recover_change_set = subparsers.add_parser(
        "recover-rebuilt-change-set",
        help="从已归档的 Plan 本地恢复 IMPLEMENT，并固定该 Plan 前驱",
    )
    recover_change_set.add_argument("run_id")
    recover_change_set.add_argument("task_id")
    recover_change_set.add_argument("plan_artifact_id")
    adopt_repair = subparsers.add_parser(
        "adopt-code-only-repair",
        help="将已批准 CODE_ONLY 修复从旧终态 run 承接到新 run，直接进入 IMPLEMENT",
    )
    adopt_repair.add_argument("source_run_id")
    adopt_repair.add_argument("target_run_id")
    adopt_repair.add_argument("task_id")
    adopt_repair.add_argument("diagnosis_artifact_id")
    adopt_repair.add_argument("plan_artifact_id")
    recover_stale = subparsers.add_parser(
        "recover-stale-submit", help="将旧 SUBMIT 检查点安全退回 PLAN"
    )
    recover_stale.add_argument("run_id")
    recover_stale.add_argument("task_id")
    recover_stale.add_argument("plan_artifact_id")

    recover_rebuilt_plan = subparsers.add_parser(
        "recover-stale-rebuilt-plan",
        help="本地克隆已验证 Plan 并将指定的旧 IMPLEMENT/SUBMIT 退回待重新 G4 审批的 PLAN",
    )
    recover_rebuilt_plan.add_argument("run_id")
    recover_rebuilt_plan.add_argument("task_id")
    recover_rebuilt_plan.add_argument("source_plan_artifact_id")
    recover_rebuilt_plan.add_argument(
        "--expected-state", required=True, choices=("IMPLEMENT", "REVIEW", "SUBMIT")
    )
    recover_rebuilt_plan.add_argument("--business-revision", required=True)
    recover_rebuilt_plan.add_argument("--tests-revision", required=True)

    recover_legacy_plan = subparsers.add_parser(
        "recover-legacy-pipeline-plan",
        help="为升级前已进入 IPIPE、缺少 pipeline_plan 的历史 run 回填冻结计划",
    )
    recover_legacy_plan.add_argument("run_id")

    optimize = subparsers.add_parser("optimize")
    optimize.add_argument("run_id")
    optimize.add_argument("operation", choices=("build", "propose", "apply"))
    optimize.add_argument("--summary")
    optimize.add_argument("--allowed-root", action="append", default=[])
    optimize.add_argument("--proposal-id")
    optimize.add_argument("--approval-id")

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("project")

    candidates = subparsers.add_parser("acceptance-candidates")
    candidates.add_argument("project")

    request_approval = subparsers.add_parser("request-approval")
    request_approval.add_argument("run_id")
    request_approval.add_argument("action")
    request_approval.add_argument("input_hash")
    request_approval.add_argument(
        "--content",
        help="产物 JSON（阶段信封或其 content），用于在审批消息里说明本次批的是什么",
    )

    await_approval = subparsers.add_parser("await-approval")
    await_approval.add_argument("run_id")
    await_approval.add_argument("approval_id")
    await_approval.add_argument("input_hash")

    watch = subparsers.add_parser("watch-approvals")
    watch.add_argument("--interval", type=float, default=10)
    watch.add_argument("--once", action="store_true")

    watch_ipipe = subparsers.add_parser("watch-ipipe")
    watch_ipipe.add_argument("--interval", type=float, default=60)
    watch_ipipe.add_argument("--once", action="store_true")

    ipipe_rerun = subparsers.add_parser(
        "ipipe-rerun", help="用已批准的 G8 重跑失败阶段或继续人工阶段"
    )
    ipipe_rerun.add_argument("run_id")
    ipipe_rerun.add_argument("stage_build_id")
    ipipe_rerun.add_argument("approval_id")
    ipipe_rerun.add_argument("input_hash")
    ipipe_rerun.add_argument(
        "--parameter",
        action="append",
        default=[],
        dest="parameters",
        help="人工阶段要填的参数名，可重复；取值由本 run 自动求出（CR 号取提交回执，"
             "产出下载命令取编译 job 的 productHttpUrl 加本地 irepo token）",
    )
    rerun_mode = ipipe_rerun.add_mutually_exclusive_group()
    rerun_mode.add_argument(
        "--print-parameters",
        action="store_true",
        help="只打印求出的参数（token 脱敏），不执行阶段",
    )
    rerun_mode.add_argument(
        "--print-input-hash",
        action="store_true",
        help="只打印本次执行的 G8 绑定哈希，用于 request-approval",
    )
    rerun_mode.add_argument(
        "--request-g8", action="store_true",
        help="保存准确脱敏参数与绑定并投递 G8；不批准、不执行阶段",
    )
    rerun_mode.add_argument(
        "--reconcile-only", action="store_true",
        help="仅 GET 核对已存在且 G8 匹配的执行意图并保存确认回执；绝不 POST",
    )

    adopt = subparsers.add_parser(
        "ipipe-adopt",
        help="认领平台已为本 run 的 CR 触发的 build 并绑定其 stage（只读）",
    )
    adopt.add_argument("run_id")
    adopt.add_argument("--module", default=None, help="要认领的模块，默认第一个业务仓")
    adopt.add_argument("--window-seconds", type=float, default=30)

    rebuild = subparsers.add_parser(
        "ipipe-rebuild",
        help="编译产物已失效时，对同一 CR 版本受控重新构建（默认只读预检；--request-g8 只投递审批）",
    )
    rebuild.add_argument("run_id")
    rebuild.add_argument("--reason", required=True, help="为什么要重新构建（进入 G8 绑定与审计）")
    rebuild.add_argument("--module", default=None, help="要重新构建的模块；多模块计划必填")
    rebuild.add_argument(
        "--event-type", default="GERRIT_CHANGE", choices=("GERRIT_CHANGE", "NEW_COMMIT"),
        help="对应 ipipe-cli trigger run --event-type；CR 的 patchset 用 GERRIT_CHANGE",
    )
    rebuild.add_argument("--approval-id", default=None, help="已批准的 G8；与 --input-hash 一起执行")
    rebuild.add_argument("--input-hash", default=None, help="G8 绑定哈希（预检输出的 input_hash）")
    rebuild_mode = rebuild.add_mutually_exclusive_group()
    rebuild_mode.add_argument(
        "--request-g8", action="store_true",
        help="保存准确材料并投递 G8；不批准、不触发",
    )
    rebuild_mode.add_argument(
        "--reconcile-only", action="store_true",
        help="仅 GET 核对已存在的触发意图并认领新构建（再换绑）；绝不 POST",
    )

    # Callable with no arguments so a session-stop hook can drive it: whoever stopped
    # the IDE may never have run a phase, which is exactly when the notice was missing.
    ide_turn = subparsers.add_parser("notify-ide-turn")
    ide_turn.add_argument("--run-id", default=None)

    ai_review = subparsers.add_parser("ai-review")
    ai_review.add_argument("operation", choices=["start", "poll"])
    ai_review.add_argument("run_id")
    ai_review.add_argument("--change-number", type=int, default=None)
    ai_review.add_argument("--revision", default=None)
    ai_review.add_argument("--conversation-id", default=None)
    ai_review.add_argument("--working-directory", default=".")
    ai_review.add_argument("--poll-interval", type=float, default=30)
    ai_review.add_argument("--max-polls", type=int, default=30)

    reissue = subparsers.add_parser("reissue-approval")
    reissue.add_argument("run_id")
    reissue.add_argument("action")
    reissue.add_argument("input_hash")

    correction = subparsers.add_parser("correct-approval-message", help="纠正同一待审批诊断的中文展示，不重开审批或延长截止时间")
    correction.add_argument("run_id")
    correction.add_argument("approval_id")
    correction.add_argument("input_hash")
    correction.add_argument("--actor", required=True)
    correction.add_argument("--reason", required=True)

    redelivery = subparsers.add_parser(
        "confirm-approval-redelivery",
        help="确认未收到指定如流审批并受控重发同一审批",
    )
    redelivery.add_argument("run_id")
    redelivery.add_argument("approval_id")
    redelivery.add_argument("input_hash")
    redelivery.add_argument("intent_id")
    redelivery.add_argument(
        "--confirm",
        required=True,
        help='必须明确填写“未收到，允许重发”',
    )
    redelivery.add_argument("--reason", required=True, help="说明已核对未收到审批的依据")
    redelivery.add_argument("--actor", required=True, help="执行确认的人员")

    repin = subparsers.add_parser("repin-profile")
    repin.add_argument("run_id")
    repin.add_argument("previous", help="被钉住的那份 profile 的副本，用于给出改动 diff")
    repin.add_argument("--request", action="store_true", help="开 PROFILE_REPIN 门")
    repin.add_argument("--approval-id", help="省略则只给出待批的 input_hash，不落账")

    migrate_spec = subparsers.add_parser(
        "migrate-workflow-spec",
        help="在审批后把旧运行迁移到当前 workflow policy",
    )
    migrate_spec.add_argument("run_id")
    migrate_spec.add_argument("--request", action="store_true", help="开 WORKFLOW_SPEC_MIGRATION 门")
    migrate_spec.add_argument("--approval-id", help="应用已批准的迁移审批")

    revise = subparsers.add_parser(
        "revise",
        help="G1–G6 选择需修改后，按修改意见重做本阶段草案并重新发起该门",
    )
    revise.add_argument("run_id")
    revise.add_argument("--approval-id", required=True, help="要重做的那道门的审批（需修改/历史驳回）")
    revise.add_argument("--feedback", help="修改意见；省略则读取如流里 `需修改 <id> 意见` 的文字")
    revise.add_argument("--actor", required=True, help="提出修改意见的人")

    rewind = subparsers.add_parser(
        "rewind",
        help="变更请求：审批后回到较早阶段重新产出（可同时改 test_repo 等 profile 字段）",
    )
    rewind.add_argument("run_id")
    rewind.add_argument("--to", dest="target", help="回退目标阶段，例如 GRILL / SPEC / TASKS")
    rewind.add_argument("--reason", help="为什么要回退、要改什么")
    rewind.add_argument("--actor", help="发起变更的人")
    rewind.add_argument("--previous-profile", help="改了 profile 时，被钉住的旧 profile 副本")
    rewind.add_argument("--request", action="store_true", help="开 CHANGE_REQUEST 门")
    rewind.add_argument("--approval-id", help="执行已批准的变更请求")

    advance = subparsers.add_parser("advance", help="推进一次状态迁移，input_hash 从审批台账里取")
    advance.add_argument("run_id")
    advance.add_argument("state", help="目标状态，例如 SPEC / IMPLEMENT / SUBMIT")
    advance.add_argument(
        "--artifact", action="append", default=[], dest="artifacts",
        help="声明本次带上的证据名，可重复；省略则由门告诉你缺哪个",
    )
    advance.add_argument("--evidence", help="其余证据上下文 JSON：workspace 收据、revision、环境指纹")
    advance.add_argument("--approval-id", help="省略则取该门最近一条 APPROVE 台账")

    companion = subparsers.add_parser(
        "submit-companion",
        help="补提已获 G7、但未推送的测试库提交（同一描述符、同一 G7，不改 run 状态）")
    companion.add_argument("run_id")
    companion.add_argument("--task", required=True)
    companion.add_argument("--approval-id")
    companion.add_argument("--icode-skill", default=None)
    submit = subparsers.add_parser("submit", help="把已评审并获 G7 的变更集提交到 iCode")
    submit.add_argument("run_id")
    submit.add_argument("--task", required=True, help="要提交的任务 id，例如 T3")
    submit.add_argument("--approval-id", help="省略则取该 run 最近一条 APPROVE 的 G7")
    submit.add_argument(
        "--icode-skill",
        default=None,
        help="system iCode skill 目录",
    )

    abandon = subparsers.add_parser("abandon-intent", help="放弃一条外部写意图，写审计收据而不是删行")
    abandon.add_argument("intent_id")
    abandon.add_argument("--reason", required=True, help="为什么不再等这条外部写")
    abandon.add_argument("--actor", required=True, help="谁做的这个决定")
    abandon.add_argument(
        "--local-error-proven", action="store_true",
        help="仅限 ipipe.rerun：校验该 intent 已有 ipipe.rerun.write_error 观察"
             "（AttributeError/TypeError/NameError 且无 http_status，即请求未发出）后，"
             "在放弃回执写入 local_error_proven=true；允许同一审批重发一次。不满足则拒绝")

    adopt_group = subparsers.add_parser(
        "adopt-group",
        help="核对已存在的如流群并认领原建群 intent，不重新创建",
    )
    adopt_group.add_argument("run_id")
    adopt_group.add_argument("group_id")
    adopt_group.add_argument("--actor", required=True)

    work_card = subparsers.add_parser("work-card", help="查询或处理动态工作卡的未知投递")
    work_card.add_argument("operation", choices=("reconcile",))
    work_card.add_argument("intent_id")
    work_card.add_argument("outcome", choices=("delivered", "abandoned"))
    work_card.add_argument("--card-id")
    work_card.add_argument("--revision", type=int)
    work_card.add_argument("--reason", required=True)
    work_card.add_argument("--actor", required=True)

    artifact = subparsers.add_parser("artifact", help="按 artifact_id 读回归档内容（含哈希校验）")
    artifact.add_argument("operation", choices=("show",))
    artifact.add_argument("artifact_id")

    args = parser.parse_args(argv)
    orchestrator = Orchestrator(args.config_root)
    # The high-level process command starts new requirements and must use a
    # confirmed per-card profile. The lower-level `start` command retains its
    # legacy compatibility during migration.
    orchestrator.require_requirement_profiles = args.command == "process"
    if args.command == "start":
        from clients.icafe_client import CafeClient

        snapshot = CafeClient().snapshot(args.requirement_id)
        if isinstance(snapshot, dict) and snapshot.get("ok") is False:
            result = {
                **snapshot,
                "ready": False,
                "project": args.project,
                "requirement_id": args.requirement_id,
            }
        else:
            result = orchestrator.start(
                args.requirement_id,
                args.project,
                requirement_snapshot=snapshot,
                fresh_token=uuid.uuid4().hex if args.fresh else None,
            )
    elif args.command == "process":
        from agent_bridge import AgentBridge, resolve_status_target

        bridge = AgentBridge(
            orchestrator,
            locks=orchestrator.recovery.locks,
            ipipe_api_factory=lambda run_id: _cli_ipipe_transport(orchestrator, run_id),
            icode_skill=args.icode_skill,
        )
        resolved = resolve_status_target(orchestrator, args.requirement_id)
        if resolved.get("reason_code") == "AMBIGUOUS_RUN":
            result = resolved
        elif resolved.get("ok"):
            if resolved.get("project") != args.project:
                result = {
                    "ok": False, "reason_code": "PROJECT_MISMATCH",
                    "run_id": resolved["run_id"], "project": resolved.get("project"),
                    "requested_project": args.project,
                }
            elif resolved.get("state") in workflow_spec.terminal_states():
                result = {
                    **resolved, "ok": False, "reason_code": "RUN_ALREADY_TERMINAL",
                    "detail": "运行已结束；使用 status 查看结果，不自动重启",
                }
            else:
                agent_host.bind_driver(orchestrator.state, resolved["run_id"])
                result = bridge.continue_run(resolved["run_id"])
                result = _deliver_worker_approval(orchestrator, result)
        else:
            from clients.icafe_client import CafeClient

            snapshot = CafeClient().snapshot(args.requirement_id)
            if isinstance(snapshot, dict) and snapshot.get("ok") is False:
                result = {
                    **snapshot,
                    "ready": False,
                    "project": args.project,
                    "requirement_id": args.requirement_id,
                }
            else:
                started = orchestrator.start(
                    args.requirement_id,
                    args.project,
                    requirement_snapshot=snapshot,
                )
                if not isinstance(started, dict) or started.get("ok") is False or not started.get("run_id"):
                    result = started
                elif started.get("state") in workflow_spec.terminal_states():
                    result = {
                        **started,
                        "ok": False,
                        "reason_code": "RUN_ALREADY_TERMINAL",
                    }
                else:
                    agent_host.bind_driver(orchestrator.state, started["run_id"])
                    result = bridge.drive(started["run_id"])
                    result = _deliver_worker_approval(orchestrator, result)
    elif args.command == "status":
        from agent_bridge import resolve_status_target
        resolved = resolve_status_target(orchestrator, args.run_id)
        if not resolved.get("ok"):
            result = resolved
        elif args.json:
            result = orchestrator.status(resolved["run_id"])
        else:
            from run_brief import build, render

            print(render(build(orchestrator, resolved["run_id"])))
            return 0
    elif args.command == "context":
        from agent_bridge import AgentBridge

        bridge = AgentBridge(
            orchestrator,
            locks=orchestrator.recovery.locks,
            ipipe_api_factory=lambda run_id: _cli_ipipe_transport(orchestrator, run_id),
        )
        result = bridge.context(args.target)
    elif args.command == "prepare-change-set":
        from agent_bridge import resolve_run_target
        from prepare_change_set import prepare

        resolved = resolve_run_target(orchestrator, args.target)
        metadata = _json_file(args.metadata_json)
        if not resolved.get("ok"):
            result = resolved
        elif metadata.get("ok") is False:
            result = metadata
        else:
            result = prepare(orchestrator, resolved["run_id"], args.job_id, metadata)
            if result.get("ok") and args.output:
                # Serialize before opening; x-mode refuses existing files and symlinks.
                encoded = json.dumps(result["content"], ensure_ascii=False, sort_keys=True) + "\n"
                output = Path(args.output).expanduser().absolute()
                try:
                    with output.open("x", encoding="utf-8") as stream:
                        stream.write(encoded)
                except FileExistsError:
                    result = {"ok": False, "reason_code": "OUTPUT_EXISTS", "output": str(output)}
                except OSError:
                    result = {"ok": False, "reason_code": "OUTPUT_WRITE_FAILED", "output": str(output)}
                else:
                    result = {**result, "output": str(output)}
    elif args.command == "approve":
        result = orchestrator.approve(args.approval_id, args.decision, args.input_hash, args.channel, args.run_id, args.responder)
    elif args.command == "resume":
        result = orchestrator.resume(args.run_id)
    elif args.command in {"drive", "continue", "submit-draft", "report-producer-block", "report-producer-conflict"}:
        from agent_bridge import resolve_run_target as _resolve_bound
        _bound = _resolve_bound(orchestrator, args.target)
        if _bound.get("ok"):
            agent_host.bind_driver(orchestrator.state, _bound["run_id"])
        from agent_bridge import AgentBridge, resolve_run_target

        bridge = AgentBridge(
            orchestrator,
            locks=orchestrator.recovery.locks,
            ipipe_api_factory=lambda run_id: _cli_ipipe_transport(orchestrator, run_id),
            icode_skill=getattr(args, "icode_skill", None),
        )
        resolved = resolve_run_target(orchestrator, args.target)
        if not resolved.get("ok"):
            result = resolved
        elif args.command == "drive":
            result = bridge.drive(resolved["run_id"])
            result = _deliver_worker_approval(orchestrator, result)
        elif args.command == "continue":
            result = bridge.continue_run(resolved["run_id"])
            result = _deliver_worker_approval(orchestrator, result)
        elif args.command == "submit-draft":
            draft = _draft_content_file(args.draft_json)
            result = draft if draft.get("ok") is False else bridge.submit_draft(
                resolved["run_id"], args.job_id, draft
            )
            result = _deliver_worker_approval(orchestrator, {**result, "run_id": resolved["run_id"]})
        elif args.command == "report-producer-block":
            report = _json_file(args.report_json)
            result = report if report.get("ok") is False else bridge.report_producer_block(
                resolved["run_id"], args.job_id, report
            )
        else:
            report = _json_file(args.report_json)
            result = report if report.get("ok") is False else bridge.report_producer_conflict(
                resolved["run_id"], args.job_id, report
            )
    elif args.command == "stop":
        from agent_bridge import resolve_run_target
        resolved = resolve_run_target(orchestrator, args.target)
        result = orchestrator.stop(resolved["run_id"]) if resolved.get("ok") else resolved
    elif args.command == "next":
        result = orchestrator.next(args.run_id)
    elif args.command == "recover-rebuilt-change-set":
        result = orchestrator.recover_rebuilt_change_set(
            args.run_id, args.task_id, args.plan_artifact_id
        )
    elif args.command == "adopt-code-only-repair":
        result = orchestrator.adopt_code_only_repair(
            args.target_run_id,
            args.source_run_id,
            args.target_run_id,
            args.task_id,
            args.diagnosis_artifact_id,
            args.plan_artifact_id,
        )
    elif args.command == "recover-stale-submit":
        result = orchestrator.recover_stale_submit(
            args.run_id, args.task_id, args.plan_artifact_id
        )
    elif args.command == "recover-stale-rebuilt-plan":
        result = orchestrator.recover_stale_rebuilt_plan(
            args.run_id, args.task_id, args.expected_state, args.source_plan_artifact_id,
            {"business": args.business_revision, "tests": args.tests_revision},
        )
    elif args.command == "recover-legacy-pipeline-plan":
        result = orchestrator.recover_legacy_pipeline_plan(args.run_id)
    elif args.command == "complete-phase":
        envelope = _json_file(args.envelope)
        result = (
            orchestrator.complete_phase(args.run_id, envelope)
            if isinstance(envelope, dict)
            else envelope
        )
        # A landed phase parks the run until someone drives the next one from the IDE.
        # Tell them in 如流, unless an approval card is already out asking the same
        # person for the same move.
        if isinstance(result, dict) and result.get("phase_complete"):
            from approval_watch import notify_ide_turn

            notice = notify_ide_turn(
                orchestrator, args.run_id, _infoflow_notify_client(orchestrator)
            )
            result = {**result, "ide_turn_notice": notice.get("reason_code")}
    elif args.command == "optimize":
        options: dict[str, Any] = {}
        if args.operation == "propose":
            loaded = _json_file(args.summary)
            if not isinstance(loaded, dict):
                result = loaded
            else:
                result = orchestrator.optimize(
                    args.run_id,
                    args.operation,
                    summary=loaded,
                    allowed_roots=[Path(item).expanduser().resolve() for item in args.allowed_root],
                )
        elif args.operation == "apply":
            result = _settle_consumed_approval(
                orchestrator, args.run_id, args.approval_id, "optimize",
                orchestrator.optimize(
                    args.run_id,
                    args.operation,
                    proposal_id=args.proposal_id,
                    approval_id=args.approval_id,
                ),
            )
        else:
            result = orchestrator.optimize(args.run_id, args.operation, **options)
    elif args.command == "acceptance-candidates":
        result = _ku_acceptance_candidates(args.project, args.config_root)
    elif args.command == "request-approval":
        result = _request_approval(
            orchestrator, args.run_id, args.action, args.input_hash, args.content
        )
    elif args.command == "await-approval":
        result = orchestrator.wait_infoflow_approval(
            args.run_id,
            args.approval_id,
            args.input_hash,
            _infoflow_approval_client(orchestrator),
            STRICT_APPROVAL_TIMEOUT_SECONDS,
        )
    elif args.command == "watch-approvals":
        result = _watch_approvals(orchestrator, args.interval, args.once)
    elif args.command == "watch-ipipe":
        result = _watch_ipipe(orchestrator, args.interval, args.once)
    elif args.command == "ipipe-rerun":
        runtime = _cli_ipipe_runtime(orchestrator, args.run_id)
        if isinstance(runtime, dict):
            result = runtime
        elif args.reconcile_only:
            result = _settle_consumed_approval(
                orchestrator, args.run_id, args.approval_id, "ipipe-rerun",
                _under_run_lease(orchestrator, args.run_id, lambda: runtime.reconcile_rerun(
                    args.stage_build_id,
                    {"approval_id": args.approval_id, "input_hash": args.input_hash},
                )),
            )
        else:
            # Only G8 preparation or execution records dependency evidence (B-21);
            # --print-parameters / --print-input-hash stay read-only.
            resolved = _stage_parameters(
                orchestrator, runtime, args.run_id, args.parameters, args.stage_build_id,
                record_evidence=bool(args.request_g8)
                or not (args.print_input_hash or args.print_parameters),
            )
            if not resolved.get("ok"):
                result = resolved
            elif args.request_g8:
                result = _deliver_worker_approval(
                    orchestrator,
                    _prepare_ipipe_g8(orchestrator, runtime, args.run_id, args.stage_build_id, resolved),
                )
            elif args.print_input_hash:
                result = runtime.rerun_input_hash(
                    args.stage_build_id, parameters=resolved["parameters"] or None
                )
            elif args.print_parameters:
                from stage_parameters import redacted

                result = {
                    "ok": True, "reason_code": "OK",
                    "parameters": redacted(resolved["parameters"]),
                    "declaration": resolved.get("declaration"),
                }
            else:
                result = _settle_consumed_approval(
                    orchestrator, args.run_id, args.approval_id, "ipipe-rerun",
                    _under_run_lease(orchestrator, args.run_id, lambda: runtime.rerun(
                        args.stage_build_id,
                        {"approval_id": args.approval_id, "input_hash": args.input_hash},
                        parameters=resolved["parameters"] or None,
                    )),
                )
    elif args.command == "ipipe-adopt":
        runtime = _cli_ipipe_runtime(orchestrator, args.run_id)
        result = runtime if isinstance(runtime, dict) else _ipipe_adopt(
            orchestrator, runtime, args.run_id, args.module, args.window_seconds
        )
    elif args.command == "ipipe-rebuild":
        import ipipe_rebuild

        runtime = _cli_ipipe_runtime(orchestrator, args.run_id)
        options = {"reason": args.reason, "module": args.module, "event_type": args.event_type}
        if isinstance(runtime, dict):
            result = runtime
        elif args.request_g8:
            result = _deliver_worker_approval(
                orchestrator,
                ipipe_rebuild.prepare_g8(orchestrator, runtime, args.run_id, **options),
            )
        elif args.approval_id or args.input_hash or args.reconcile_only:
            if not (args.approval_id and args.input_hash):
                result = {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": args.run_id,
                          "detail": "执行与对账都需要 --approval-id 和 --input-hash"}
            else:
                result = ipipe_rebuild.execute(
                    orchestrator, runtime, args.run_id, args.approval_id, args.input_hash,
                    reconcile_only=args.reconcile_only,
                )
        else:
            planned = ipipe_rebuild.plan(orchestrator, runtime, args.run_id, **options)
            result = ipipe_rebuild.public_plan(planned) if planned.get("ok") else planned
    elif args.command == "ai-review":
        from cli_transport import ProcessTransport

        runtime = orchestrator.ai_review_runtime(
            args.run_id,
            argv_transport=ProcessTransport(),
            working_directory=args.working_directory,
            poll_interval=args.poll_interval,
            max_polls=args.max_polls,
        )
        if isinstance(runtime, dict):
            result = runtime
        elif args.operation == "start":
            result = runtime.start(args.change_number, args.revision)
        else:
            result = runtime.poll(args.conversation_id)
    elif args.command == "notify-ide-turn":
        from approval_watch import notify_every_parked_run, notify_ide_turn

        client = _infoflow_notify_client(orchestrator)
        result = (
            notify_ide_turn(orchestrator, args.run_id, client)
            if args.run_id
            else notify_every_parked_run(orchestrator, client)
        )
    elif args.command == "reissue-approval":
        result = _reissue_approval(orchestrator, args.run_id, args.action, args.input_hash)
    elif args.command == "correct-approval-message":
        from approval_delivery import correct_approval_message
        result = correct_approval_message(
            orchestrator, args.run_id, args.approval_id, args.input_hash,
            actor=args.actor, reason=args.reason,
            client=_infoflow_notify_client(orchestrator),
        )
    elif args.command == "confirm-approval-redelivery":
        result = _confirm_approval_redelivery(
            orchestrator,
            args.run_id,
            args.approval_id,
            args.input_hash,
            args.intent_id,
            args.confirm,
            args.reason,
            args.actor,
        )
    elif args.command == "repin-profile":
        import profile_repin

        if args.approval_id:
            result = _settle_consumed_approval(
                orchestrator, args.run_id, args.approval_id, "repin-profile",
                profile_repin.apply(orchestrator, args.run_id, args.approval_id, args.previous),
            )
        elif args.request:
            from approval_delivery import ComateApprovalClient

            result = profile_repin.request(
                orchestrator, args.run_id, args.previous,
                comate_client=ComateApprovalClient(),
                infoflow_client=_infoflow_approval_client(orchestrator),
            )
        else:
            result = profile_repin.plan(orchestrator, args.run_id, args.previous)
    elif args.command == "migrate-workflow-spec":
        import workflow_spec_migration

        if args.approval_id:
            result = workflow_spec_migration.apply(orchestrator, args.run_id, args.approval_id)
        elif args.request:
            from approval_delivery import ComateApprovalClient

            result = workflow_spec_migration.request(
                orchestrator,
                args.run_id,
                comate_client=ComateApprovalClient(),
                infoflow_client=_infoflow_approval_client(orchestrator),
            )
        else:
            result = workflow_spec_migration.plan(orchestrator, args.run_id)
    elif args.command == "revise":
        import run_change

        result = run_change.revise(
            orchestrator, args.run_id, args.approval_id, feedback=args.feedback, actor=args.actor
        )
    elif args.command == "rewind":
        import run_change

        if args.approval_id:
            result = _settle_consumed_approval(
                orchestrator, args.run_id, args.approval_id, "rewind",
                run_change.apply(orchestrator, args.run_id, args.approval_id),
            )
        elif not (args.target and args.reason and args.actor):
            result = {"ok": False, "reason_code": "INVALID_INPUT",
                      "detail": "rewind 需要 --to、--reason、--actor（执行时只需 --approval-id）"}
        elif args.request:
            from approval_delivery import ComateApprovalClient

            result = run_change.request(
                orchestrator, args.run_id, args.target, args.reason, args.actor,
                args.previous_profile,
                comate_client=ComateApprovalClient(),
                infoflow_client=_infoflow_approval_client(orchestrator),
            )
        else:
            result = run_change.plan(
                orchestrator, args.run_id, args.target, args.reason, args.actor,
                args.previous_profile,
            )
    elif args.command == "advance":
        result = _advance(
            orchestrator, args.run_id, args.state, args.artifacts, args.evidence, args.approval_id
        )
    elif args.command == "submit":
        result = _submit(orchestrator, args.run_id, args.task, args.approval_id, args.icode_skill)
    elif args.command == "submit-companion":
        result = _submit_missing_companion(
            orchestrator, args.run_id, args.task, args.approval_id, args.icode_skill)
    elif args.command == "abandon-intent":
        result = _abandon_intent(orchestrator, args.intent_id, args.reason, args.actor,
                                 args.local_error_proven)
    elif args.command == "adopt-group":
        from clients.infoflow_group_client import InfoflowGroupClient

        result = orchestrator.collaboration_session(
            InfoflowGroupClient()
        ).adopt_pending_group(args.run_id, args.group_id, args.actor)
    elif args.command == "work-card":
        from work_card import reconcile

        result = reconcile(
            orchestrator, args.intent_id, args.outcome,
            actor=args.actor, reason=args.reason,
            card_id=args.card_id, revision=args.revision,
        )
    elif args.command == "artifact":
        result = _artifact_show(orchestrator, args.artifact_id)
    else:
        result = orchestrator.preflight(args.project)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return _cli_exit_code(result)


@guard_execution
def _submit(
    orchestrator: Any, run_id: str, task_id: str, approval_id: str | None, icode_skill: str,
    icode_runtime: Any | None = None,
) -> dict[str, Any]:
    """Drive one task's submission end to end.

    The wiring this needs -- the reviewed descriptor, the worktree binding the boundary
    preflights, a process transport -- was previously assembled by hand for every
    submission, which is both tedious and the kind of step that gets a detail wrong under
    pressure. It is derived here instead, from the artifacts and the pinned profile.

    `icode_runtime` is normally None (the real runtime is constructed here); the durable
    worker and tests pass one in so the same derivation drives a supplied runtime.
    """
    from cli_transport import ProcessTransport
    from submit_descriptor import _ownership_rows, build_and_archive, owned_row

    # Reject impossible invocations before descriptor construction can commit either
    # repository. Exact input-hash validation still happens after the descriptor is built.
    current = orchestrator.status(run_id)
    if current.get("state") != "SUBMIT":
        return {"ok": False, "reason_code": "INVALID_STATE", "run_id": run_id,
                "state": current.get("state")}
    if not any(
        isinstance(record, dict)
        and gate_of(record.get("action")) == "G7"
        and record.get("effective_decision") == "APPROVE"
        for record in orchestrator.approvals.for_run(run_id)
    ):
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id}

    built = build_and_archive(orchestrator, run_id, task_id)
    if not built.get("ok"):
        return built
    descriptor = built["descriptor"]
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    repositories = [*(profile.get("business_repos") or []), profile.get("test_repo") or {}]
    repository = next(
        (item for item in repositories if isinstance(item, dict) and item.get("module") == descriptor["module"]),
        None,
    )
    rows = _ownership_rows(orchestrator, run_id)
    row = owned_row(rows, task_id, repository.get("path")) if isinstance(repository, dict) else None
    if not isinstance(row, dict):
        return {"ok": False, "reason_code": "WORKTREE_NOT_OWNED", "run_id": run_id, "task_id": task_id}
    # Descriptor construction may select a clean same-workspace checkout whose HEAD
    # exactly matches the reviewed revision. Keep the durable ownership row as the
    # authority, while bind the runtime to the actual descriptor path; otherwise the
    # runtime looks up the fallback path in the ownership map and rejects a valid
    # reviewed checkout as WORKTREE_NOT_REGISTERED.
    binding = {
        "run_id": run_id,
        "baseline_revision": row["baseline_revision"],
        "module": repository["module"],
        "target_branch": repository["branch"],
        "repo_path": row["repo_path"],
        "task_id": task_id,
        "owner_token": row["owner_token"],
        "worktree_path": descriptor["repo_path"],
        "ownership_worktree_path": row["worktree_path"],
    }
    approval = _g7_approval(orchestrator, run_id, approval_id, descriptor["input_hash"])
    if approval is None:
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id,
                "input_hash": descriptor["input_hash"]}
    companion = _companion_binding(orchestrator, run_id, task_id, profile, descriptor, rows)
    if isinstance(companion, dict) and companion.get("ok") is False:
        return companion
    bindings = {descriptor["repo_path"]: binding}
    if companion is not None:
        bindings[companion["worktree_path"]] = companion
    runtime = icode_runtime if icode_runtime is not None else orchestrator.icode_runtime(
        run_id,
        worktree_bindings=bindings,
        owner=descriptor["owner"],
        system_skill_path=icode_skill or agent_host.skill_path("icode"),
        argv_transport=ProcessTransport(),
        submission_policy=profile.get("submission_policy"),
    )
    # Contract 3.2: every repository of the change set is its own iCode intent, and the
    # attempt as a whole is SUBMITTED only once all of them are confirmed. The transition
    # to IPIPE happens inside submit_to_ipipe, after the last push, so it can only follow
    # a complete set; a partial one is reported as such and goes to F, never onward.
    import submission_set
    change_set_ids = [descriptor["change_set_id"]]
    if companion is not None:
        change_set_ids.append(f"{descriptor['change_set_id']}:companion:{companion['module']}")
        pushed = _submit_companion(orchestrator, run_id, runtime, descriptor, approval,
                                   companion["worktree_path"])
        if not pushed.get("ok"):
            return _route_partial({**pushed, "submission_set": submission_set.evaluate(
                orchestrator.state, run_id, change_set_ids, descriptor["revision_set_id"],
                descriptor["change_set_id"])}, orchestrator, run_id)
    result = orchestrator.submit_to_ipipe(run_id, descriptor, approval, icode_runtime=runtime,
                                          change_set_ids=change_set_ids)
    aggregate = submission_set.evaluate(
        orchestrator.state, run_id, change_set_ids, descriptor["revision_set_id"],
        descriptor["change_set_id"])
    if isinstance(result, dict):
        result = _route_partial({**result, "submission_set": aggregate}, orchestrator, run_id)
    return _settle_consumed_approval(
        orchestrator, run_id, approval.get("approval_id"), "submit", result,
    )


def _attempt_change_set_ids(orchestrator: Any, run_id: str, change_set: dict[str, Any]) -> list[str]:
    """Every repository the approved descriptor carries a new commit for (contract 3.2).

    The revision_set entry the descriptor does not push is a companion exactly when its
    revision is not the owned baseline of that repository -- the rule `_companion_binding`
    applies -- so the set is derived here from the descriptor and ownership, not taken from
    the caller. A descriptor is only built from owned worktrees of both repositories
    (`build_and_archive`), so an entry without an ownership row cannot come from the
    approved path; it is left to the caller's list rather than invented here.
    """
    from submit_descriptor import _ownership_rows, owned_row

    ids = [change_set["change_set_id"]]
    entries = [entry for entry in (change_set.get("revision_set") or {}).values()
               if isinstance(entry, dict) and not (
                   entry.get("module") == change_set.get("module")
                   and entry.get("revision") == change_set.get("commit_revision"))]
    if not entries:
        return ids
    pinned = orchestrator._runtime_profile(run_id)
    profile = pinned.get("profile") or {} if pinned.get("ok") else {}
    repositories = [*(profile.get("business_repos") or []), profile.get("test_repo") or {}]
    rows = _ownership_rows(orchestrator, run_id)
    for entry in entries:
        repository = next((item for item in repositories if isinstance(item, dict)
                           and item.get("module") == entry.get("module")), None)
        row = (owned_row(rows, change_set.get("task_id"), repository.get("path"))
               if isinstance(repository, dict) and change_set.get("task_id") else None)
        if not isinstance(row, dict) or entry.get("revision") == row.get("baseline_revision"):
            continue
        ids.append(f"{change_set['change_set_id']}:companion:{entry['module']}")
    return ids


def _route_partial(result: dict[str, Any], orchestrator: Any, run_id: str) -> dict[str, Any]:
    """Contract 3.2: a PARTIAL attempt goes to F (DIAGNOSE), once; PENDING stays in SUBMIT."""
    aggregate = result.get("submission_set") or {}
    if result.get("ok") or aggregate.get("status") != "PARTIAL":
        return result
    # A refused push (permission, CLI) is retried in SUBMIT once the cause is fixed: the
    # refusal receipt is superseded and the retry chains off it, keeping the confirmed
    # sibling. Only an AMBIGUOUS repository needs a person, hence DIAGNOSE.
    if not any(row.get("intent_state") == "AMBIGUOUS" for row in aggregate.get("repositories", [])):
        return result
    evidence = {"submission_set": {
        "change_set_id": aggregate.get("change_set_id"),
        "revision_set_id": aggregate.get("revision_set_id"),
        "repositories": [{k: row.get(k) for k in ("change_set_id", "intent_id", "intent_state")}
                         for row in aggregate.get("repositories", [])]}}
    routed = orchestrator.route_failure(run_id, "SUBMIT_PARTIAL", evidence)
    return {**result, "routed": routed}


def _companion_binding(
    orchestrator: Any, run_id: str, task_id: str, profile: dict[str, Any],
    descriptor: dict[str, Any], rows: Any,
) -> dict[str, Any] | None:
    """The owned worktree of the revision_set entry the descriptor does not push.

    None when that repository has no new commit (its revision is still the owned
    baseline): a task that changes only one repository has nothing to carry along.
    """
    from submit_descriptor import owned_row

    entries = [entry for entry in (descriptor.get("revision_set") or {}).values()
               if not (entry.get("module") == descriptor["module"]
                       and entry.get("revision") == descriptor["commit_revision"])]
    if len(entries) != 1:
        return None
    entry = entries[0]
    repositories = [*(profile.get("business_repos") or []), profile.get("test_repo") or {}]
    repository = next((item for item in repositories
                       if isinstance(item, dict) and item.get("module") == entry["module"]), None)
    row = owned_row(rows, task_id, repository.get("path")) if isinstance(repository, dict) else None
    if not isinstance(row, dict):
        return {"ok": False, "reason_code": "WORKTREE_NOT_OWNED", "run_id": run_id,
                "task_id": task_id, "module": entry["module"]}
    if entry["revision"] == row["baseline_revision"]:
        return None
    from submit_descriptor import _matching_clean_worktree
    path = _matching_clean_worktree(row["worktree_path"], entry["revision"]) or row["worktree_path"]
    return {
        "run_id": run_id,
        "baseline_revision": row["baseline_revision"],
        "module": repository["module"],
        "target_branch": repository["branch"],
        "repo_path": row["repo_path"],
        "task_id": task_id,
        "owner_token": row["owner_token"],
        "worktree_path": str(Path(path).expanduser().resolve()),
        "ownership_worktree_path": row["worktree_path"],
    }


def _submit_companion(
    orchestrator: Any, run_id: str, runtime: Any, descriptor: dict[str, Any],
    approval: dict[str, Any], worktree_path: str,
) -> dict[str, Any]:
    """Push the companion commit and archive its receipt next to the primary's."""
    submit = getattr(runtime, "submit_companion", None)
    if not callable(submit):
        return {"ok": False, "reason_code": "ICODE_RUNTIME_MISMATCH", "run_id": run_id}
    pushed = submit(descriptor, approval, worktree_path)
    if not isinstance(pushed, dict) or not pushed.get("ok"):
        return pushed if isinstance(pushed, dict) else {
            "ok": False, "reason_code": "ICODE_RECEIPT_INVALID", "run_id": run_id}
    content = json.dumps(pushed, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    orchestrator.artifacts.put(run_id, "companion-submission", content, {
        "change_set_id": descriptor["change_set_id"],
        "revision_set_id": descriptor["revision_set_id"],
        "module": pushed.get("module"),
        "commit_revision": pushed.get("commit_revision"),
    })
    return pushed


def _submit_missing_companion(
    orchestrator: Any, run_id: str, task_id: str, approval_id: str | None, icode_skill: str,
) -> dict[str, Any]:
    """Recover a run whose G7-approved test commit was never pushed.

    Before `_submit` carried the companion, a task changing both repositories reached
    IPIPE with only the business CR. The descriptor and its G7 still name the test
    commit, so pushing it under that same approval is the write the G7 authorized; the
    run state is not touched.
    """
    from cli_transport import ProcessTransport
    from pipeline_plan import current_descriptors
    from submit_descriptor import _ownership_rows

    entry = current_descriptors(orchestrator, run_id).get(task_id)
    if not isinstance(entry, dict):
        return {"ok": False, "reason_code": "CHANGE_SET_MISSING", "run_id": run_id}
    artifact = entry["artifact"]
    descriptor = {**entry["descriptor"], "input_hash": artifact["sha256"],
                  "reviewed_artifact_id": artifact["artifact_id"]}
    approval = _g7_approval(orchestrator, run_id, approval_id, descriptor["input_hash"])
    if approval is None or approval.get("effective_decision") != "APPROVE":
        return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id,
                "input_hash": descriptor["input_hash"]}
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    companion = _companion_binding(orchestrator, run_id, task_id, profile, descriptor,
                                   _ownership_rows(orchestrator, run_id))
    if companion is None:
        return {"ok": True, "reason_code": "NO_COMPANION", "run_id": run_id}
    if companion.get("ok") is False:
        return companion
    runtime = orchestrator.icode_runtime(
        run_id,
        worktree_bindings={companion["worktree_path"]: companion},
        owner=descriptor["owner"],
        system_skill_path=icode_skill or agent_host.skill_path("icode"),
        argv_transport=ProcessTransport(),
        submission_policy=profile.get("submission_policy"),
    )
    return _submit_companion(orchestrator, run_id, runtime, descriptor,
                             {"approval_id": approval["approval_id"],
                              "input_hash": descriptor["input_hash"]},
                             companion["worktree_path"])


def _replayed_submit_intent(
    approvals: Any, run_id: str, intent: Any, expected: dict[str, Any]
) -> bool:
    """An earlier G7's completed push, replayed under a later G7 for the same change.

    The iCode intent records the approval that authorized the push. A later descriptor
    for the same change set and revision set (one that gained a pinned baseline, say)
    is approved by a new G7, but the push it describes already happened. That receipt
    is still this change's receipt when every identity field matches and the approval
    the intent names was itself an APPROVE of G7 for the intent's own input hash.
    """
    if not isinstance(intent, dict):
        return False
    volatile = {"input_hash", "approval_id"}
    if {k: v for k, v in intent.items() if k not in volatile} != {
        k: v for k, v in expected.items() if k not in volatile
    }:
        return False
    record = approvals.get(intent.get("approval_id")) if isinstance(intent.get("approval_id"), str) else None
    return (
        isinstance(record, dict)
        and record.get("run_id") == run_id
        and gate_of(record.get("action")) == "G7"
        and record.get("effective_decision") == "APPROVE"
        and record.get("input_hash") == intent.get("input_hash")
    )


def _g7_approval(
    orchestrator: Any, run_id: str, approval_id: str | None, input_hash: str
) -> dict[str, Any] | None:
    """The approved G7 bound to exactly these bytes."""
    if isinstance(approval_id, str) and approval_id:
        record = orchestrator.approvals.get(approval_id)
        return record if isinstance(record, dict) else None
    for record in reversed(orchestrator.approvals.for_run(run_id)):
        if (
            gate_of(record.get("action")) == "G7"
            and record.get("effective_decision") == "APPROVE"
            and record.get("input_hash") == input_hash
        ):
            return record
    return None


def _cli_exit_code(result: Any) -> int:
    if not isinstance(result, dict):
        return 1
    if result.get("ready") is False or result.get("ok") is False:
        return 1
    if result.get("state") == "RUN_NOT_FOUND":
        return 1
    reason = result.get("reason_code")
    return 0 if reason in {
        None, "OK", "READY", "REBUILT_CHANGE_SET_RECOVERED", "STALE_REBUILT_PLAN_RECOVERED",
        "SUBMISSION_RECORDED", "LEGACY_PIPELINE_PLAN_MIGRATED", "PIPELINE_PLAN_PRESENT", "NO_COMPANION",
        "CODE_VERSION_CHANGED",
        "PARKED", "WORK_CARD_RECONCILED", "WORK_CARD_ABANDONED",
        "CHANGE_SET_PREPARED",
    } else 1


def _infoflow_notify_client(orchestrator: Orchestrator) -> Any:
    """The plain-message client. Distinct from `_infoflow_approval_client`, which
    speaks the approval-card protocol and has no `send_markdown`/`send_group_markdown`
    at all — passing it where a notice is sent fails at send time, not at wiring."""
    from clients.infoflow_bot_client import InfoflowBotClient

    return InfoflowBotClient(journal_path=orchestrator.config_root / "infoflow-replies.jsonl")


def _infoflow_approval_client(orchestrator: Orchestrator) -> Any:
    from approval_delivery import InfoflowApprovalTransport
    from clients.infoflow_approval_client import InfoflowApprovalClient
    from clients.infoflow_bot_client import InfoflowBotClient
    from clients.infoflow_reply_client import InfoflowReplyConsumer, InfoflowReplyJournal

    def progress_provider(run_id: str) -> dict[str, Any]:
        from progress_snapshot import build as build_progress

        return build_progress(orchestrator, run_id)

    journal_path = orchestrator.config_root / "infoflow-replies.jsonl"
    return InfoflowApprovalClient(
        InfoflowApprovalTransport(
            orchestrator.state,
            InfoflowBotClient(journal_path=journal_path),
            progress_provider=progress_provider,
            reply_consumer=InfoflowReplyConsumer(
                InfoflowReplyJournal(journal_path),
                # Cards go to the run's group, so a reply typed there has to be
                # readable — and only from that group.
                group_resolver=lambda run_id: group_id_for_run(orchestrator.state, run_id),
            ),
        )
    )


def _request_approval(
    orchestrator: Orchestrator,
    run_id: str,
    action: str,
    input_hash: str,
    content_path: str | None = None,
) -> dict[str, Any]:
    """Request a human gate over the configured channels.

    The member policy is the union of the profile's role members: a gate that only
    reached one role could be answered without the others ever seeing it.
    """
    from approval_delivery import ComateApprovalClient

    context = _approval_context(
        orchestrator, run_id, action=action, content_path=content_path, input_hash=input_hash
    )
    if "error" in context:
        return context["error"]
    return orchestrator.request_infoflow_approval(
        run_id,
        action,
        input_hash,
        member_policy=context["member_policy"],
        evidence=context["evidence"],
        comate_client=ComateApprovalClient(),
        infoflow_client=_infoflow_approval_client(orchestrator),
    )


def _reissue_approval(
    orchestrator: Orchestrator, run_id: str, action: str, input_hash: str
) -> dict[str, Any]:
    """Reopen a gate whose deadline passed, keeping the bound input hash."""
    context = _approval_context(orchestrator, run_id, action=action, input_hash=input_hash)
    if "error" in context:
        return context["error"]
    return orchestrator.reissue_infoflow_approval(
        run_id,
        action,
        input_hash,
        member_policy=context["member_policy"],
        evidence=context["evidence"],
        infoflow_client=_infoflow_approval_client(orchestrator),
        allow_workflow_spec_migration=migration_exempt(action),
    )


def _confirm_approval_redelivery(
    orchestrator: Orchestrator,
    run_id: str,
    approval_id: str,
    input_hash: str,
    intent_id: str,
    confirmation: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Close one unknown Infoflow attempt and retry that same approval.

    This is the explicit escape hatch for an operator who checked the remote
    channel and confirms that the card was not received.  It never creates a
    new approval, changes its hash, or deletes the old intent; the abandonment
    receipt makes the retry lineage auditable.
    """
    if confirmation != "未收到，允许重发":
        return {"ok": False, "reason_code": "REDISPATCH_CONFIRMATION_REQUIRED", "run_id": run_id}
    if not isinstance(reason, str) or not reason.strip():
        return {"ok": False, "reason_code": "REDISPATCH_REASON_REQUIRED", "run_id": run_id}
    if not isinstance(actor, str) or not actor.strip():
        return {"ok": False, "reason_code": "REDISPATCH_ACTOR_REQUIRED", "run_id": run_id}

    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict):
        return {"ok": False, "reason_code": "APPROVAL_NOT_FOUND", "run_id": run_id}
    # Refuse before abandoning an intent or caching approval context: only the
    # migration gate may be redelivered while the run's workflow pin is drifted.
    if not migration_exempt(approval.get("action")):
        blocked = execution_guard(orchestrator.state, run_id)
        if blocked is not None:
            return blocked
    if approval.get("run_id") != run_id or approval.get("input_hash") != input_hash:
        return {
            "ok": False,
            "reason_code": "APPROVAL_BINDING_MISMATCH",
            "run_id": run_id,
            "approval_id": approval_id,
        }

    intent = orchestrator.state.intent_by_id(intent_id)
    if not isinstance(intent, dict):
        return {"ok": False, "reason_code": "INTENT_NOT_FOUND", "run_id": run_id}
    if intent.get("run_id") != run_id or intent.get("operation") != "approval.delivery.infoflow":
        return {"ok": False, "reason_code": "INTENT_BINDING_MISMATCH", "run_id": run_id}
    payload = intent.get("payload")
    if not isinstance(payload, dict) or payload.get("approval_id") != approval_id:
        return {"ok": False, "reason_code": "INTENT_APPROVAL_MISMATCH", "run_id": run_id}
    if orchestrator.state.result_by_idempotency_key(intent["idempotency_key"]) is not None:
        return {"ok": False, "reason_code": "INTENT_ALREADY_RECONCILED", "run_id": run_id}

    if any(
        entry.get("channel") == "infoflow"
        and entry.get("payload_hash") == input_hash
        for entry in approval.get("delivery_receipts") or []
        if isinstance(entry, dict)
    ):
        return {"ok": False, "reason_code": "APPROVAL_DELIVERY_ALREADY_CONFIRMED", "run_id": run_id}

    context = _approval_context(
        orchestrator,
        run_id,
        action=gate_of(approval.get("action") or ""),
        input_hash=input_hash,
    )
    if "error" in context:
        return context["error"]

    # A redelivery confirmation can arrive after the original approval deadline.
    # Reusing that row would post a card that is already expired: the transport
    # correctly resolves it as TIMEOUT, while the client would then misclassify
    # the non-pending result as APPROVAL_REQUEST_RESPONSE_INVALID. Close the old
    # attempt and open the normal, fresh retry instead. The old card remains
    # auditable, and any click on it is correctly treated as late.
    try:
        deadline = datetime.fromisoformat(
            str(approval.get("deadline_at")).replace("Z", "+00:00")
        )
        expired = deadline.tzinfo is not None and datetime.now(timezone.utc) >= deadline
    except (TypeError, ValueError):
        expired = False
    if expired:
        timed_out = orchestrator.approvals.timeout(
            approval_id,
            input_hash,
            run_id=run_id,
            state_store=orchestrator.state,
        )
        if timed_out.get("effective_decision") != "TIMEOUT":
            return {
                "ok": False,
                "reason_code": timed_out.get("reason_code", "APPROVAL_TIMEOUT_FAILED"),
                "run_id": run_id,
                "approval_id": approval_id,
            }
        abandoned = orchestrator.state.abandon_intent(intent_id, reason, actor)
        if abandoned.get("status") not in {"ABANDONED", "ALREADY_ABANDONED"}:
            return {
                "ok": False,
                "reason_code": "INTENT_ABANDON_FAILED",
                "run_id": run_id,
                "approval_id": approval_id,
                "intent_id": intent_id,
                "detail": abandoned,
            }
        retry = orchestrator.reissue_infoflow_approval(
            run_id,
            gate_of(approval.get("action") or ""),
            input_hash,
            member_policy=approval.get("member_policy") or context["member_policy"],
            evidence=context["evidence"],
            infoflow_client=_infoflow_approval_client(orchestrator),
            allow_workflow_spec_migration=migration_exempt(approval.get("action")),
        )
        return {
            **retry,
            "ok": retry.get("reason_code") in {None, "OK", "PENDING"},
            "reason_code": (
                "APPROVAL_EXPIRED_REISSUED"
                if retry.get("approval_id")
                else retry.get("reason_code", "APPROVAL_REISSUE_FAILED")
            ),
            "previous_approval_id": approval_id,
            "previous_intent_id": intent_id,
        }

    abandoned = orchestrator.state.abandon_intent(intent_id, reason, actor)
    if abandoned.get("status") not in {"ABANDONED", "ALREADY_ABANDONED"}:
        return {
            "ok": False,
            "reason_code": "INTENT_ABANDON_FAILED",
            "run_id": run_id,
            "intent_id": intent_id,
            "detail": abandoned,
        }

    payload = {
        "approval_id": approval_id,
        "run_id": run_id,
        "action": approval["action"],
        "input_hash": input_hash,
        "deadline_at": approval.get("deadline_at"),
        "evidence": context["evidence"],
        "member_policy": approval.get("member_policy") or context["member_policy"],
    }
    outcome = orchestrator._deliver_approval_channel(
        run_id,
        approval_id,
        "infoflow",
        _infoflow_approval_client(orchestrator),
        {"channel": "infoflow", "approval": payload},
        input_hash,
    )
    if outcome.get("reason_code") != "OK":
        orchestrator.approvals.record_delivery_failure(
            approval_id,
            {
                "reason_code": outcome.get("reason_code"),
                "channels": ["infoflow"],
                "error_types": [outcome["error_type"]] if outcome.get("error_type") else [],
                "retry_allowed": bool(outcome.get("retry_allowed")),
            },
        )
    else:
        orchestrator.approvals.record_delivery_failure(approval_id, None)
    return {
        "ok": outcome.get("reason_code") == "OK",
        "reason_code": (
            "APPROVAL_REDELIVERY_CONFIRMED"
            if outcome.get("reason_code") == "OK"
            else outcome.get("reason_code")
        ),
        "run_id": run_id,
        "approval_id": approval_id,
        "input_hash": input_hash,
        "superseded_intent_id": intent_id,
        "replacement_intent_id": (
            orchestrator.state.intent_by_idempotency_key(
                f"approval.delivery:{approval_id}:infoflow:after:{intent_id}"
            ) or {}
        ).get("intent_id"),
        "delivery": outcome,
    }


def _approval_context(
    orchestrator: Orchestrator,
    run_id: str,
    *,
    action: str | None = None,
    content_path: str | None = None,
    input_hash: str | None = None,
) -> dict[str, Any]:
    """Member policy and card evidence for a run's gates.

    The member policy is the union of the profile's role members: a gate that only
    reached one role could be answered without the others ever seeing it.
    """
    cache_key = (
        f"approval.context:{run_id}:{action or '-'}:{input_hash or '-'}"
    )
    cached = orchestrator.state.idempotency_result(cache_key)
    if isinstance(cached, dict):
        return cached
    loaded = orchestrator._runtime_profile(run_id)
    if not loaded.get("ok"):
        return {"error": loaded}
    channels = loaded["profile"].get("approval_channels")
    role_members = channels.get("role_members") if isinstance(channels, dict) else None
    if not isinstance(role_members, dict):
        return {"error": {"ok": False, "reason_code": "MEMBER_CONFIRMATION_REQUIRED", "run_id": run_id}}
    members = sorted({email for values in role_members.values() for email in values})
    if not members:
        return {"error": {"ok": False, "reason_code": "MEMBER_CONFIRMATION_REQUIRED", "run_id": run_id}}
    events = orchestrator.state.events(run_id)
    payload = events[0]["payload"] if events and isinstance(events[0].get("payload"), dict) else {}
    binding = payload.get("collaboration_binding")
    binding = binding if isinstance(binding, dict) else {}
    card_id = binding.get("card_id", payload.get("requirement_id", ""))

    # Preserve an already-delivered legacy CLI evidence envelope only when the
    # durable delivery claim proves it was built from this exact evidence.  The
    # claim stores a digest rather than the old payload, so reconstructing the
    # canonical legacy shape is the safe compatibility check; a nearby approval
    # or a changed digest must use the current material path instead.
    for approval in orchestrator.approvals.for_run(run_id):
        if approval.get("action") != action or approval.get("input_hash") != input_hash:
            continue
        intent = orchestrator.state.intent_by_idempotency_key(
            f"approval.delivery:{approval.get('approval_id')}:comate"
        )
        claim = intent.get("payload") if isinstance(intent, dict) else {}
        digest = claim.get("canonical_payload_sha256") if isinstance(claim, dict) else None
        from approval_summary import _GATES, _hash
        gate_name = gate_of(str(action))
        subject, effect = _GATES.get(gate_name, ("", ""))
        legacy_evidence = {
            "project": payload.get("project", ""),
            "card_id": card_id,
            "card_title": binding.get("card_title", ""),
            "card_url": _icafe_url(card_id),
            "group_name": binding.get("group_name", ""),
            "documents": _requirement_documents(loaded["profile"]),
            "gate": {
                "subject": subject,
                "effect": effect,
                "summary": [
                    f"群名 {binding.get('group_name', '')}",
                    f"成员 {'、'.join(binding.get('member_snapshot') or [])}",
                    *(
                        f"{role} {'、'.join(people)}"
                        for role, people in sorted((binding.get("roles") or {}).items())
                    ),
                ],
                "content_hash": payload.get("requirement_snapshot", {}).get("content_hash"),
            },
        }
        legacy_approval = {
            key: approval[key]
            for key in (
                "approval_id", "run_id", "action", "input_hash",
                "deadline_at", "member_policy",
            )
            if key in approval
        }
        if digest == _hash({"channel": "comate", "approval": {
            **legacy_approval, "evidence": legacy_evidence,
        }}):
            return {
                "member_policy": approval.get("member_policy") or {},
                "evidence": legacy_evidence,
            }

    # Explicit content is a producer-supplied envelope. Rebuild its identity
    # from the current action instead of trusting fields the file reports about
    # itself; a foreign run/task or altered content must fail closed.
    if content_path:
        try:
            from run_brief import _read_only_next
            from worker_driver import build_envelope
            document = json.loads(Path(content_path).read_text(encoding="utf-8"))
            content = document.get("content") if isinstance(document, dict) else None
            content = content if isinstance(content, dict) else document
            current = _read_only_next(orchestrator, run_id)
            expected = build_envelope(current, content)
            if (
                not isinstance(document, dict)
                or document.get("run_id") != run_id
                or document.get("action_id") != current.get("action_id")
                or document.get("source_event_id") != current.get("source_event_id")
                or document.get("task_id") != current.get("task_id")
                or document.get("content_hash") != expected.get("content_hash")
                or document.get("approval_input_hash") != expected.get("approval_input_hash")
                or expected.get("approval_input_hash") != input_hash
            ):
                return {"error": {"ok": False, "reason_code": "APPROVAL_CONTENT_MISMATCH", "run_id": run_id}}
        except (OSError, ValueError, TypeError, KeyError):
            return {"error": {"ok": False, "reason_code": "APPROVAL_CONTENT_INVALID", "run_id": run_id}}

    from approval_summary import pinned_context
    if content_path:
        gate = _gate_evidence(action, payload, binding, content_path, input_hash)
    else:
        gate = {"gate": pinned_context(orchestrator, run_id, str(action or ""), input_hash or "")}
        if gate["gate"].get("material_status") == "MISSING":
            if str(action or "") in {"G0", "G4", "G5", "G9", "G10"}:
                reason = (
                    "APPROVAL_CONTENT_MISMATCH"
                    if input_hash != payload.get("g0_input_hash") and str(action or "") == "G0"
                    else "APPROVAL_MATERIAL_MISSING"
                )
                return {"error": {"ok": False, "reason_code": reason, "run_id": run_id}}
            # Keep the old summary path for gates whose controller action is
            # already the exact binding but has no specialized archive reader.
            fallback = _gate_evidence(action, payload, binding, content_path, input_hash)
            gate = fallback if "error" in fallback or fallback.get("gate", {}).get("summary") else gate
    if "error" in gate:
        return {"error": {"ok": False, "reason_code": gate["error"], "run_id": run_id}}
    gate_payload = gate["gate"]
    if content_path:
        gate_payload = {
            **gate_payload,
            "material_status": "PINNED",
        }
    if gate_payload.get("material_status") == "MISSING":
        return {
            "error": {
                "ok": False,
                "reason_code": "APPROVAL_MATERIAL_MISSING",
                "run_id": run_id,
            }
        }
    if gate_payload.get("material_status") == "PINNED":
        packet_dir = orchestrator.config_root / "approval-materials"
        packet_dir.mkdir(parents=True, exist_ok=True)
        material_stem = f"{run_id}-{action}-{(input_hash or '')[:16]}"
        packet_path = packet_dir / f"{material_stem}.json"
        preview_path = packet_dir / f"{material_stem}.md"
        material = gate_payload.get("_review_content") or {}
        packet = {
            "run_id": run_id,
            "gate": action,
            "input_hash": input_hash,
            "material": material,
        }
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2)
        if not packet_path.exists() or packet_path.read_text(encoding="utf-8") != encoded:
            packet_path.write_text(encoded, encoding="utf-8")
        from phase_document import content_lines

        draft = material.get("draft") if isinstance(material, dict) else None
        preview_lines = [
            "# 本次审批材料预览",
            "",
            f"- run_id：`{run_id}`",
            f"- gate：`{action}`",
            f"- input_hash：`{input_hash}`",
            "",
        ]
        if isinstance(draft, dict):
            if isinstance(draft.get("spec"), dict):
                preview_lines.extend(content_lines(draft["spec"], "SPEC"))
                if isinstance(draft.get("dag"), dict):
                    preview_lines.extend([
                        "",
                        "## 关联 DAG（随 Spec 一并核对）",
                        "",
                        *content_lines(draft["dag"], "TASKS"),
                    ])
            else:
                preview_lines.extend(content_lines(draft, None))
        elif gate_payload.get("summary"):
            # Controller gates (G7, G8, ...) have no DraftContent; their pinned summary
            # is the material the card shows, so the preview shows it too.
            preview_lines.extend(["## 审批内容", "", *(f"- {line}" for line in gate_payload["summary"])])
        else:
            preview_lines.extend(["## 材料缺失", "", "未找到与 input_hash 匹配的 DraftContent。"])
        preview = "\n".join(preview_lines).rstrip() + "\n"
        if not preview_path.exists() or preview_path.read_text(encoding="utf-8") != preview:
            preview_path.write_text(preview, encoding="utf-8")
        refs = [
            {"label": "本次审批材料包（JSON）", "ref": str(packet_path)},
            {"label": "本次审批可读预览（Markdown）", "ref": str(preview_path)},
        ]
        refs.extend(ref for ref in gate_payload.get("references", []) if ref.get("ref") != str(packet_path))
        refs = [ref for ref in refs if ref.get("ref") != str(preview_path) or ref["label"] == "本次审批可读预览（Markdown）"]
        gate_payload = {
            **gate_payload,
            "references": refs,
        }
    result = {
        "member_policy": {"comate": members, "infoflow": members},
        "evidence": {
            "project": payload.get("project", ""),
            "card_id": card_id,
            "card_title": binding.get("card_title", ""),
            "card_url": _icafe_url(card_id),
            "group_name": binding.get("group_name", ""),
            "documents": _requirement_documents(loaded["profile"]),
            "gate": gate_payload,
        },
    }
    orchestrator.state.save_idempotency_result(cache_key, result)
    return result


def _retry_exhausted_hint(events: list[dict[str, Any]]) -> str:
    from run_change import rewind_available

    state = events[-1].get("state") if events else None
    if rewind_available(events, state):
        targets = "/".join(sorted(workflow_spec.REWIND_TARGETS[state]))
        return f"重发次数已用完：核对审批成员后，可发起 rewind 回到 {targets} 换一道新门，或 stop 结束运行"
    return "重发次数已用完：核对审批成员与投递后 stop 结束运行，或新建运行"


def _gate_evidence(
    action: str | None,
    payload: dict[str, Any],
    binding: dict[str, Any],
    content_path: str | None,
    input_hash: str | None = None,
) -> dict[str, Any]:
    """What this gate decides, summarized from the content the hash covers.

    A summary that is not derived from the approved content would let the message
    and the binding drift apart, so the content is hashed here and, when the file
    is a phase envelope, checked against the hash the gate is being opened on.
    """
    from approval_summary import content_summary, gate_intent

    gate: dict[str, Any] = dict(gate_intent(action))
    if content_path:
        try:
            document = json.loads(Path(content_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "APPROVAL_CONTENT_INVALID"}
        envelope = document if isinstance(document, dict) else {}
        content = envelope.get("content")
        content = content if isinstance(content, dict) else document
        if not isinstance(content, dict):
            return {"error": "APPROVAL_CONTENT_INVALID"}
        content_hash = _content_hash_of(content)
        declared = envelope.get("content_hash")
        bound = envelope.get("approval_input_hash")
        if isinstance(declared, str) and declared != content_hash:
            return {"error": "APPROVAL_CONTENT_MISMATCH"}
        if isinstance(bound, str) and isinstance(input_hash, str) and bound != input_hash:
            return {"error": "APPROVAL_CONTENT_MISMATCH"}
        gate["summary"] = content_summary(content)
        gate["content_hash"] = content_hash
    elif str(action) == "G0":
        snapshot = payload.get("requirement_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        roles = binding.get("roles") if isinstance(binding.get("roles"), dict) else {}
        gate["summary"] = [
            f"群名 {binding.get('group_name', '-')}",
            f"成员 {'、'.join(binding.get('member_snapshot') or []) or '-'}",
            *(f"{role} {'、'.join(people)}" for role, people in sorted(roles.items())),
        ]
        gate["content_hash"] = str(snapshot.get("content_hash") or "")
    return {"gate": gate}


def _content_hash_of(content: Any) -> str:
    from phase_protocol import _canonical_json

    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _watch_approvals(orchestrator: Orchestrator, interval: float, once: bool) -> dict[str, Any]:
    """Keep landing 如流 decisions while the operator is away from the CLI."""
    from approval_watch import ApprovalWatcher
    from code_version import changed_since, fingerprint

    started = fingerprint()
    watcher = ApprovalWatcher(
        orchestrator,
        lambda: _infoflow_approval_client(orchestrator),
        _infoflow_notify_client(orchestrator),
        reporter=lambda outcome: print(json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True),
        code_changed=None if once else changed_since(started),
    )
    settled = watcher.run(interval, iterations=1 if once else None)
    return _watch_result(settled, watcher, started)


def _watch_ipipe(orchestrator: Orchestrator, interval: float, once: bool) -> dict[str, Any]:
    """Report a build's outcome while nobody is watching the CLI."""
    from ipipe_watch import IpipeWatcher

    def runtime(run_id: str) -> Any:
        return _cli_ipipe_runtime(orchestrator, run_id)

    from code_version import changed_since, fingerprint

    started = fingerprint()
    watcher = IpipeWatcher(
        orchestrator,
        runtime,
        _infoflow_notify_client(orchestrator),
        reporter=lambda outcome: print(json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True),
        code_changed=None if once else changed_since(started),
    )
    settled = watcher.run(interval, iterations=1 if once else None)
    return _watch_result(settled, watcher, started)


def _watch_result(settled: list[Any], watcher: Any, started: str) -> dict[str, Any]:
    """A watcher that stopped because its code changed exits cleanly for the supervisor."""
    if getattr(watcher, "stopped_for_code_change", False):
        return {"ok": True, "reason_code": "CODE_VERSION_CHANGED", "settled": settled,
                "started_code_version": started}
    return {"ok": True, "reason_code": "OK", "settled": settled}


def _draft_content_file(path: str) -> dict[str, Any]:
    """Load only producer DraftContent; reject an ArtifactEnvelope pasted by mistake."""
    loaded = _json_file(path)
    if loaded.get("ok") is False:
        return loaded
    envelope_fields = {"schema_version", "action_id", "source_event_id", "content_hash", "approval_id", "approval_input_hash"}
    if envelope_fields.intersection(loaded):
        return {"ok": False, "reason_code": "DRAFT_CONTENT_ONLY"}
    return loaded


def _deliver_worker_approval(orchestrator: Orchestrator, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result
    approval_required = result.get("reason_code") == "APPROVAL_REQUIRED"
    parked_approval = result.get("reason_code") == "PARKED" and result.get("parked") == "APPROVAL_WAIT"
    if not approval_required and not parked_approval:
        return result
    gate = result.get("gate")
    input_hash = result.get("approval_input_hash")
    # WorkerDriver returns its normalized approval action under `decision.action`
    # for parked approvals. Older controller paths returned gate/hash at the top
    # level. Normalize both shapes here; otherwise G0/G1/... is computed but no
    # ApprovalLedger row or Infoflow/Comate delivery is created.
    decision = result.get("decision")
    action = decision.get("action") if isinstance(decision, dict) else None
    if isinstance(action, dict):
        gate = gate or action.get("required_human_gate")
        input_hash = input_hash or action.get("input_hash")
    if not isinstance(gate, str) or not isinstance(input_hash, str):
        return result
    approval = _request_approval(orchestrator, result.get("run_id"), gate, input_hash)
    approval_succeeded = isinstance(approval, dict) and (
        approval.get("ok") is True
        or (
            approval.get("status") == "PENDING"
            and isinstance(approval.get("approval_id"), str)
            and bool(approval.get("approval_id"))
        )
    )
    if not approval_succeeded:
        approval_failure = approval if isinstance(approval, dict) else {
            "reason_code": "APPROVAL_DELIVERY_INCOMPLETE",
        }
        return {
            **result,
            **approval_failure,
            "ok": False,
            **({"draft_saved": True} if approval_required else {}),
            "worker_reason_code": result.get("reason_code"),
            "worker_result": result,
            "approval": approval,
        }
    if approval_required:
        return {
            **result, "ok": True, "reason_code": "PARKED", "parked": "APPROVAL_WAIT",
            "draft_saved": True, "approval": approval,
        }
    return {**result, "approval": approval}


def _cli_ipipe_transport(orchestrator: Orchestrator, run_id: str) -> Any:
    """Build the run-bound iPipe transport, or return None so the worker parks safely."""
    from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport
    from clients.ku_client import resolve_username

    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return None
    repos = pinned["profile"].get("business_repos") or []
    user = resolve_username([item["path"] for item in repos if item.get("path")])
    if not user:
        return None
    return IpipeApiClient(IpipeHttpTransport(), current_user=user)


def _cli_ipipe_runtime(orchestrator: Orchestrator, run_id: str) -> Any:
    """Build the run-bound iPipe runtime for explicit CLI actions."""
    from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport
    from clients.ku_client import resolve_username

    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    repos = pinned["profile"].get("business_repos") or []
    user = resolve_username([item["path"] for item in repos if item.get("path")])
    if not user:
        return {"ok": False, "reason_code": "IPIPE_CURRENT_USER_REQUIRED", "run_id": run_id}
    api = IpipeApiClient(IpipeHttpTransport(), current_user=user)
    return orchestrator.ipipe_runtime(run_id, api)


def _icafe_url(card_id: Any) -> str:
    if not isinstance(card_id, str) or "-" not in card_id:
        return ""
    return f"https://console.cloud.baidu-int.com/devops/icafe/issue/{card_id}/show"


def _requirement_documents(profile: Any) -> list[dict[str, str]]:
    """Link the profile's KU sources so an approver can read what is being approved."""
    sources = profile.get("knowledge_sources") if isinstance(profile, dict) else None
    if not isinstance(sources, list):
        return []
    documents = []
    for source in sorted(
        (item for item in sources if isinstance(item, dict) and item.get("provider") == "ku"),
        key=lambda item: item.get("priority", 0),
    ):
        url = source.get("repository")
        if not isinstance(url, str) or not url:
            continue
        label = source.get("label") or source.get("search_scope") or url
        documents.append({"label": str(label), "url": url})
    return documents


def _json_file(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {"ok": False, "reason_code": "JSON_FILE_REQUIRED"}
    try:
        payload = json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "reason_code": "JSON_FILE_INVALID"}
    if not isinstance(payload, dict):
        return {"ok": False, "reason_code": "JSON_FILE_INVALID"}
    return payload


def _abandon_intent(
    orchestrator: Orchestrator, intent_id: str, reason: str, actor: str,
    local_error_proven: bool = False,
) -> dict[str, Any]:
    """Stop waiting on an external write, on the record.

    The x86bgw CDN-URL run had no command for this, so it was done three times with
    `DELETE FROM external_intents` against the live database. `StateStore.abandon_intent`
    writes an abandonment receipt instead; this is the supported way to reach it, and
    it turns the store's `ValueError`s into the CLI's reason codes so an
    `INTENT_ALREADY_RECONCILED` exits 1 rather than printing a traceback.
    """
    try:
        return {"ok": True, "reason_code": "OK", **orchestrator.state.abandon_intent(
            intent_id, reason, actor, local_error_proven=local_error_proven)}
    except ValueError as error:
        return {"ok": False, "reason_code": str(error), "intent_id": intent_id}


def _artifact_show(orchestrator: Orchestrator, artifact_id: str) -> dict[str, Any]:
    """Read one archived artifact back, integrity check included.

    `status` lists a run's artifacts but never their content, so reading the spec the
    gate is citing meant guessing the path under `artifacts/` and `cat`-ing the file --
    which bypasses the hash check that decides whether those bytes are still evidence.
    Content comes back as text when it decodes as UTF-8; when it does not, the bytes
    are left out rather than mangled, since the point of this command is fidelity.
    """
    loaded = orchestrator.artifacts.get(artifact_id)
    if not loaded.get("valid"):
        return {"ok": False, **loaded}
    content = loaded.get("content")
    shaped = {key: value for key, value in loaded.items() if key != "content"}
    try:
        shaped["content"] = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError:
        shaped["content_encoding"] = "BINARY"
    return {"ok": True, **shaped}


def _advance(
    orchestrator: Orchestrator,
    run_id: str,
    next_state: str,
    artifacts: list[str],
    evidence_path: str | None,
    approval_id: str | None,
) -> dict[str, Any]:
    """Drive one transition, taking the gate's hash from the ledger rather than a paste.

    `advance` was reachable only from Python, so the x86bgw run moved the state machine
    by hand-pasting `input_hash` into a `python3 -c` call, and mistyped it. The hash is
    not the operator's to know: it is whatever the approval was bound to, and the ledger
    already holds it next to the approval id. So both are read from the approved row for
    this transition's gate -- newest wins, since a reissued gate supersedes the one that
    timed out -- and a transition with no gate needs neither.

    The artifact list stays the operator's assertion (`--artifact NAME`, repeatable).
    Deriving it was the tempting half of this command and would have been a lie: the
    evidence gate checks artifact *names* (`requirement-snapshot`, `task-plan`,
    `ipipe-evidence`) and those names are not artifact kinds -- nothing ever calls
    `put(kind="requirement-snapshot")` -- so a name-to-kind guess would produce a gate
    that reads as though it verified the archive and did not. Omit them and the gate
    answers `MISSING_ARTIFACT` with exactly what it wants.

    `--evidence FILE` carries the rest of a context the gate needs and this command
    cannot invent: workspace receipts, revision sets, environment fingerprints.
    """
    current = orchestrator.status(run_id)
    if current["state"] == "RUN_NOT_FOUND":
        return current
    evidence: dict[str, Any] = {}
    if evidence_path:
        loaded = _json_file(evidence_path)
        if loaded.get("ok") is False:
            return loaded
        evidence = loaded
    if artifacts:
        carried = evidence.get("artifacts")
        carried = carried if isinstance(carried, list) else []
        evidence["artifacts"] = list(dict.fromkeys([*carried, *artifacts]))
    gate = requirement_for(next_state, current["state"]).approval_gate
    if gate is not None and not (approval_id or evidence.get("approval_id")):
        approved = [
            row for row in orchestrator.approvals.for_run(run_id)
            if gate_of(row["action"]) == gate and row["effective_decision"] == "APPROVE"
        ]
        if not approved:
            return {
                "run_id": run_id, "state": current["state"], "passed": False,
                "action": next_state, "reason_code": "APPROVAL_REQUIRED",
                "missing_evidence": [gate],
            }
        approval_id = approved[-1]["approval_id"]
    if approval_id:
        record = orchestrator.approvals.get(approval_id)
        if record is None:
            return {"ok": False, "reason_code": "APPROVAL_NOT_FOUND", "run_id": run_id}
        evidence["approval_id"] = approval_id
        # Only when the operator did not bring one: an `--evidence` file describing a
        # WORKSPACE binding computes its own hash, and `advance` compares the two.
        evidence.setdefault("input_hash", record["input_hash"])
    return _settle_consumed_approval(
        orchestrator, run_id, approval_id, "advance",
        orchestrator.advance(run_id, next_state, evidence),
    )


def _settle_consumed_approval(
    orchestrator: Any, run_id: str, approval_id: Any, command: str, result: Any
) -> Any:
    """Close the resume handoff of an approval an explicit command just consumed.

    `approve` records an APPROVAL_RESUME handoff against the run's then-latest event.
    A command that spends that approval itself appends a newer event, so the handoff can
    never resume anything and used to make the next `continue` fail with
    MULTIPLE_RESUME_HANDOFFS. Only a successful result closes it, and only handoffs of
    exactly this approval; completion is idempotent and audited.
    """
    state = getattr(orchestrator, "state", None)
    if (
        state is not None
        and isinstance(result, dict)
        and result.get("ok") is not False
        and (result.get("ok") is True or result.get("reason_code") == "OK")
        and isinstance(approval_id, str) and approval_id
    ):
        state.complete_approval_handoffs(
            run_id, approval_id, actor=command, reason="APPROVAL_CONSUMED"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
