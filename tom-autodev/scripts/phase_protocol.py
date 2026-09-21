from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from artifact_store import ArtifactStore
from collaboration import (
    intake_prerequisites,
    intake_prerequisites_valid,
)
from persistence_policy import ensure_persistable, validate_evidence_refs
from phase_document import render_phase_markdown
from project_registry import load_profile
import repair_policy
from requirement_snapshot import AcceptanceValueError, normalized_acceptance_ids
from schema_validator import validate_named_schema

import workflow_spec


_log = logging.getLogger(__name__)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STABLE_DEPENDENCY_REASONS = frozenset({
    "INTENT_CONFLICT", "RECEIPT_CONFLICT", "ARTIFACT_CONFLICT",
    "ARTIFACT_COMPONENT_INVALID", "ARTIFACT_PATH_ESCAPE", "ARTIFACT_ENVELOPE_INVALID",
    "CONTENT_HASH_MISMATCH", "SCHEMA_INVALID", "EVIDENCE_REF_INVALID",
    "EVIDENCE_REQUIRED", "PERSISTENCE_SECRET_REJECTED", "KNOWLEDGE_RECEIPT_INVALID",
    "APPROVAL_NOT_FOUND", "APPROVAL_INPUT_MISMATCH", "APPROVAL_RUN_MISMATCH",
})
_DRAFT_KEYS = frozenset(
    {
        "action_id", "source_event_id", "host", "run_id", "phase", "task_id",
        "schema_version", "input_hash", "content_hash", "source_revisions",
        "parent_artifact_hash", "knowledge_doc_id", "knowledge_url", "knowledge_version",
        "icafe_comment_id", "evidence_refs", "approval_id", "approval_input_hash", "content",
    }
)

# Phase / controller definitions, KU titles and task-scoped titles are all derived from
# the single workflow spec (workflow_spec.py). Editing a phase's skill/schema/gate/target
# means editing the spec; tests/test_workflow_spec.py pins the reconstruction.
_PHASES: dict[str, dict[str, Any]] = workflow_spec.phase_definitions()

_CONTROLLERS = workflow_spec.controller_definitions()

_TITLE = workflow_spec.titles()
# Phases whose KU title carries the task id. Everything else is run scoped, and
# `_phase_attempt` has to count in the same scope or a re-entered phase would claim a
# title that already exists.
_TASK_SCOPED_TITLES = workflow_spec.task_scoped_titles()

# Operations that a fresh `publish_phase` for the same artifact drives again by itself.
# Every one of them is keyed on the artifact's own content, so calling `publish_phase`
# with the same title and content hash reuses the existing intent row (see
# `state_store.intent`, which returns the stored row for a repeated idempotency key)
# and re-attempts the same external write.
_PUBLISH_REDRIVEN_OPERATIONS = frozenset({
    "knowledge.publish-phase",
    "ku.run-root.create",
    "ku.document.create",
    "ku.document.publish",
    "ku.document.publish.reflush",
    "ku.index.edit",
    "icafe.comment",
})


def _redriven_by_publish(operation: Any) -> bool:
    if not isinstance(operation, str):
        return False
    suffix = ".reconcile"
    base = operation[: -len(suffix)] if operation.endswith(suffix) else operation
    return base in _PUBLISH_REDRIVEN_OPERATIONS


class PhaseProtocol:
    def __init__(
        self,
        *,
        state_store: Any,
        artifact_store: ArtifactStore,
        knowledge_sync: Any | None,
        approval_ledger: Any | None,
        evidence_gate: Any,
        transition_policy: Any,
    ):
        self.state = state_store
        self.artifacts = artifact_store
        self.knowledge = knowledge_sync
        self.approvals = approval_ledger
        self.evidence_gate = evidence_gate
        self.transitions = transition_policy

    def next(self, run_id: str) -> dict[str, Any]:
        try:
            return self._next(run_id)
        except Exception as error:
            return _failure(_exception_reason(error, "PHASE_PROTOCOL_INVALID"), run_id=run_id)

    def _repin(self, events: list[dict[str, Any]]) -> Any:
        """The approved profile re-pin for the run these events belong to, if any."""
        from profile_repin import record_for

        run_id = events[0].get("run_id") if events else None
        return record_for(self.state, run_id) if isinstance(run_id, str) and run_id else None

    def _blocking_pending(self, run_id: str) -> list[dict[str, Any]]:
        """Pending intents that no further phase work can settle.

        A pending intent means the control plane does not know whether an external
        write landed, and for `icode.submit` or `ipipe.trigger` the only way to find
        out is to go and ask, so the run stops and waits for recovery. The KU publish
        steps are not like that: `complete` publishes through the very operations
        listed in `_PUBLISH_REDRIVEN_OPERATIONS`, each keyed on the artifact's own
        content hash, so calling it again re-attempts exactly the write that is open
        and closes the intent either way.

        Treating those as terminal is what parked the x86bgw CDN-URL run nine times:
        KU's read-back lagged behind its own successful write, `_verify_child` failed
        with no receipt, and the reconciliation that would have settled the intent sat
        behind this guard. The only way out was to call `publish_phase` by hand.
        """
        return [
            pending
            for pending in self.state.pending_intents(run_id)
            if not _redriven_by_publish(pending.get("operation"))
        ]

    def _foreign_publish_intent(
        self, run_id: str, title: str, content_hash: str
    ) -> dict[str, Any] | None:
        """A pending publish that publishing *this* artifact would leave open.

        `_blocking_pending` is only safe while the open rows belong to the artifact
        about to be published. A publish intent naming another title or content hash
        is keyed on that other artifact, so this publish would not touch it -- and
        letting it through would carry a second artifact past an external write that
        nobody ever confirmed, which is what the absolute guard was there to stop.
        """
        title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
        for pending in self.state.pending_intents(run_id):
            if pending.get("operation") != "knowledge.publish-phase":
                continue
            payload = pending.get("payload")
            if not isinstance(payload, dict):
                return pending
            if (
                payload.get("title_hash") != title_hash
                or payload.get("content_hash") != content_hash
            ):
                return pending
        return None

    def _next(self, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            return _failure("INVALID_INPUT")
        events = self.state.events(run_id)
        if not events:
            return _failure("RUN_NOT_FOUND", run_id=run_id)
        current = events[-1]
        state = current.get("state")
        if state in workflow_spec.terminal_states():
            return {
                **_failure("TERMINAL_STATE", run_id=run_id),
                "state": state,
                "host": "comate",
                "stop": True,
            }
        profile_error = _pinned_profile_error(events, self._repin(events))
        if profile_error is not None:
            return _failure(profile_error, run_id=run_id)
        if self._blocking_pending(run_id):
            return _failure("RECOVERY_REQUIRED", run_id=run_id, retry_allowed=False)
        definition = _PHASES.get(state)
        controller_definition = _CONTROLLERS.get(state)
        if definition is None and controller_definition is None:
            return _failure("INVALID_STATE", run_id=run_id, state=state)
        selected = definition or controller_definition
        if state == "INTAKE":
            payload = current.get("payload")
            snapshot = payload.get("requirement_snapshot") if isinstance(payload, dict) else None
            if (
                not intake_prerequisites_valid(payload, run_id)
                or validate_named_schema(snapshot, "requirement-snapshot")
            ):
                return _failure("INTAKE_PREREQUISITES_INVALID", run_id=run_id, state=state)
        if state == "IPIPE":
            binding_error = self._ipipe_binding_error(events, current.get("payload"))
            if binding_error is not None:
                return _failure(binding_error, run_id=run_id, state=state)
        task_id = self._task_id(run_id, state, current)
        if state in {"PLAN", "IMPLEMENT", "REVIEW"} and task_id is None:
            return _failure("TASK_FRONTIER_EMPTY", run_id=run_id, state=state)
        predecessor = self._predecessor(run_id, selected.get("predecessor"), task_id)
        if selected.get("predecessor") and predecessor is None:
            return _failure("PREDECESSOR_REQUIRED", run_id=run_id, state=state, task_id=task_id)
        source_revisions = self._source_revisions(current, predecessor)
        if selected.get("revisions") and not _valid_revisions(source_revisions):
            return _failure("SOURCE_REVISION_REQUIRED", run_id=run_id, state=state, task_id=task_id)
        target = selected.get("target")
        if isinstance(target, str):
            transition = self.transitions.validate(state, target)
            if not transition.get("allowed"):
                return _failure(transition.get("reason_code", "INVALID_TRANSITION"), run_id=run_id, state=state)
        input_artifacts = [] if predecessor is None else [_artifact_reference(predecessor)]
        parent_hash = predecessor["envelope"]["content_hash"] if predecessor is not None else None
        action_inputs = {
            "run_id": run_id,
            "source_event_id": current["event_id"],
            "state": state,
            "phase": state,
            "task_id": task_id,
            "profile_hash": _pinned_profile_hash(events, self._repin(events)),
            "input_artifacts": input_artifacts,
            "parent_artifact_hash": parent_hash,
            "source_revisions": source_revisions,
            **({"intake_prerequisites": _intake_prerequisites(events)} if state == "INTAKE" else {}),
            **({"controller_binding": _ipipe_controller_binding(current.get("payload"))} if state == "IPIPE" else {}),
            **({"baseline_revisions": source_revisions} if state == "IMPLEMENT" else {}),
        }
        input_hash = (
            current["payload"]["g0_input_hash"]
            if state == "INTAKE"
            else _canonical_hash(action_inputs)
        )
        # Change-class routing. express GRILL is auto-derived (deterministic decision-log,
        # no model, no G1) when the card already carries acceptance — otherwise it falls
        # back to the full skill path. An "ungated" phase (express TASKS) still runs the
        # model but waives its human gate; the owner's G0 express declaration covers it.
        mode = workflow_spec.phase_mode_for_run(events, state)
        child_skill = selected.get("skill")
        human_gate = selected.get("gate")
        auto_content: dict[str, Any] | None = None
        if mode == "auto" and state == "GRILL":
            snapshot = _intake_snapshot(events)
            acceptance = snapshot.get("acceptance") if isinstance(snapshot, dict) else None
            if isinstance(acceptance, list) and acceptance:
                auto_content = _auto_grill_decision_log(snapshot)
                child_skill = None
                human_gate = None
        elif mode == "ungated":
            human_gate = None
        action = {
            "ok": True,
            "reason_code": "OK",
            "action_id": _canonical_hash({"identity": action_inputs, "input_hash": input_hash}),
            "run_id": run_id,
            "source_event_id": current["event_id"],
            "state": state,
            "phase": state,
            "host": "comate",
            "child_skill": child_skill,
            "controller": selected.get("controller"),
            "task_id": task_id,
            "input_artifacts": input_artifacts,
            "input_hash": input_hash,
            "parent_artifact_hash": parent_hash,
            "source_revisions": source_revisions,
            **({"baseline_revisions": source_revisions} if state == "IMPLEMENT" else {}),
            **({"controller_binding": _ipipe_controller_binding(current.get("payload"))} if state == "IPIPE" else {}),
            "source_evidence_refs": _source_evidence_refs(events, predecessor),
            "required_human_gate": human_gate,
            "allowed_side_effects": _allowed_side_effects(state, controller_definition is not None),
            "completion_predicate": _completion_predicate(state, selected.get("schema")),
            "result_schema": selected.get("schema"),
            "target_state": target,
            **(
                {"content": _intake_snapshot(events)} if state == "INTAKE"
                else ({"content": auto_content} if auto_content is not None else {})
            ),
        }
        key = _action_key(run_id, current["event_id"], state, task_id,
                          _pinned_profile_hash(events, self._repin(events)))
        existing = self.state.idempotency_result(key)
        if existing is not None:
            return existing if existing == action else _failure("ACTION_CONFLICT", run_id=run_id, state=state)
        self.state.save_idempotency_result(key, action)
        return action

    def validate_result(self, action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._validate_result(action, result)
        except Exception as error:
            return _failure(_exception_reason(error, "RESULT_VALIDATION_FAILED"))

    def _validate_result(self, action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action, dict) or not action.get("ok") or not isinstance(result, dict):
            return _failure("INVALID_INPUT")
        if action.get("controller") not in {None, "intake"}:
            return _failure("CONTROLLER_ACTION_NOT_COMPLETABLE")
        if set(result) != _DRAFT_KEYS:
            return _failure("ENVELOPE_INVALID")
        if result.get("host") != "comate" or action.get("host") != "comate":
            return _failure("HOST_NOT_COMATE")
        if result.get("run_id") != action.get("run_id"):
            return _failure("RUN_ID_MISMATCH")
        if result.get("source_event_id") != action.get("source_event_id"):
            return _failure("STALE_ACTION")
        events = self.state.events(action["run_id"])
        if not events or events[-1].get("event_id") != action.get("source_event_id"):
            return _failure("STALE_ACTION")
        action_key = _action_key(
            action["run_id"], action["source_event_id"], action.get("state"),
            action.get("task_id"), _pinned_profile_hash(events, self._repin(events)),
        )
        issued = self.state.idempotency_result(action_key)
        if issued is None or issued != action:
            return _failure("ACTION_ID_MISMATCH")
        if result.get("action_id") != action.get("action_id"):
            return _failure("ACTION_ID_MISMATCH")
        collaboration_error = self._collaboration_session_error(action, result)
        if collaboration_error is not None:
            return _failure(collaboration_error)
        if result.get("phase") != action.get("phase"):
            return _failure("PHASE_MISMATCH")
        if result.get("task_id") != action.get("task_id"):
            return _failure("TASK_ID_MISMATCH")
        content = result.get("content")
        if isinstance(content, dict) and action.get("task_id") is not None:
            content_task = content.get("task_id")
            if content_task is not None and content_task != action["task_id"]:
                return _failure("TASK_ID_MISMATCH")
        if result.get("schema_version") != "1":
            return _failure("SCHEMA_VERSION_INVALID")
        if result.get("input_hash") != action.get("input_hash"):
            return _failure("INPUT_HASH_MISMATCH")
        if result.get("parent_artifact_hash") != action.get("parent_artifact_hash"):
            return _failure("PARENT_ARTIFACT_MISMATCH")
        if action.get("phase") == "IMPLEMENT":
            if (
                not _valid_revisions(result.get("source_revisions"))
                or not isinstance(content, dict)
                or result.get("source_revisions") != content.get("revisions")
            ):
                return _failure("SOURCE_REVISION_MISMATCH")
        elif result.get("source_revisions") != action.get("source_revisions"):
            return _failure("SOURCE_REVISION_MISMATCH")
        if action.get("phase") != "INTAKE" and result.get("parent_artifact_hash") is None:
            return _failure("PARENT_ARTIFACT_MISMATCH")
        schema_name = action.get("result_schema")
        if not isinstance(schema_name, str):
            return _failure("RESULT_SCHEMA_REQUIRED")
        schema_issues = validate_named_schema(content, schema_name)
        if schema_issues:
            return {
                **_failure("SCHEMA_INVALID"),
                "schema_errors": [{"path": issue.path, "kind": issue.kind} for issue in schema_issues],
            }
        predecessor_error = self._predecessor_binding_error(action, content)
        if predecessor_error is not None:
            return _failure(predecessor_error)
        if action.get("phase") == "INTAKE" and content != action.get("content"):
            return _failure("REQUIREMENT_CHANGED")
        # A controller-authored action carries the exact content it expects back (INTAKE's
        # snapshot, an express auto-derived GRILL log). The submitted content must match it
        # so the deterministic artifact cannot be tampered on the way to completion.
        if (
            action.get("phase") != "INTAKE"
            and action.get("content") is not None
            and content != action.get("content")
        ):
            return _failure("AUTO_CONTENT_MISMATCH")
        expected_hash = _canonical_hash(content)
        if result.get("content_hash") != expected_hash:
            return _failure("CONTENT_HASH_MISMATCH")
        if not _valid_hash(result.get("content_hash")):
            return _failure("CONTENT_HASH_INVALID")
        try:
            references = validate_evidence_refs(result.get("evidence_refs"))
            ensure_persistable(result)
        except ValueError as error:
            return _failure(str(error))
        if not references and action.get("phase") != "INTAKE":
            return _failure("EVIDENCE_REQUIRED")
        if any(result.get(key) is not None for key in ("knowledge_doc_id", "knowledge_url", "knowledge_version", "icafe_comment_id")):
            return _failure("DRAFT_REMOTE_IDENTITY_INVALID")
        expected_approval_hash = _approval_input_hash(
            action, result["content_hash"], result.get("source_revisions")
        )
        if result.get("approval_input_hash") != expected_approval_hash:
            return _failure("APPROVAL_INPUT_MISMATCH")
        approval_error = self._approval_error(action, result)
        if approval_error is not None:
            return _failure(approval_error)
        return {"ok": True, "reason_code": "OK", "draft": json.loads(_canonical_json(result))}

    def _collaboration_session_error(
        self, action: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        if action.get("phase") != "INTAKE":
            return None
        events = self.state.events(action.get("run_id"))
        payload = events[0].get("payload") if events else None
        run_id = action.get("run_id")
        if not isinstance(run_id, str) or not intake_prerequisites_valid(payload, run_id):
            return "INTAKE_PREREQUISITES_INVALID"
        binding = payload["collaboration_binding"]
        completed = self.state.result_by_idempotency_key(binding["session_idempotency_key"])
        if not isinstance(completed, dict) or completed.get("operation") != "infoflow.group.create":
            return "COLLABORATION_SESSION_INVALID"
        expected_request = {
            key: binding[key]
            for key in (
                "run_id", "project", "card_id", "profile_hash", "group_name", "owner",
                "member_snapshot", "roles", "friendlyLevel", "card_content_hash",
            )
        }
        expected_request.update({
            "input_hash": action.get("input_hash"),
            "approval_id": result.get("approval_id"),
        })
        intent = completed.get("intent")
        receipt = completed.get("receipt")
        response = receipt.get("response") if isinstance(receipt, dict) else None
        if not isinstance(intent, dict) or intent.get("payload") != expected_request:
            return "COLLABORATION_SESSION_INVALID"
        if not isinstance(response, dict) or not isinstance(response.get("group_id"), str) or not response["group_id"]:
            return "COLLABORATION_SESSION_INVALID"
        expected_response = {
            "run_id": run_id,
            "group_name": binding["group_name"],
            "owner": binding["owner"],
            "roles": binding["roles"],
            "member_snapshot": binding["member_snapshot"],
            "approval_requests": [action.get("input_hash")],
            "g0_approval_id": result.get("approval_id"),
        }
        if any(response.get(key) != value for key, value in expected_response.items()):
            return "COLLABORATION_SESSION_INVALID"
        return None

    def _predecessor_binding_error(self, action: dict[str, Any], content: Any) -> str | None:
        if not isinstance(content, dict):
            return "SCHEMA_INVALID"
        phase = action.get("phase")
        run_id = action["run_id"]
        task_id = action.get("task_id")
        definition = _PHASES.get(phase, {})
        predecessor = self._predecessor(run_id, definition.get("predecessor"), task_id)
        predecessor_content = predecessor.get("envelope", {}).get("content") if predecessor else None

        if phase == "SPEC":
            root = self.artifacts.latest_phase(run_id, "INTAKE", None)
            if not root.get("valid"):
                return "TRACEABILITY_MISMATCH"
            try:
                snapshot_points = set(normalized_acceptance_ids(
                    root["envelope"]["content"].get("acceptance")
                ))
                grill_points = set(normalized_acceptance_ids(
                    predecessor_content.get("acceptance_delta", [])
                    if isinstance(predecessor_content, dict) else []
                ))
            except AcceptanceValueError:
                return "TRACEABILITY_MISMATCH"
            # A card may arrive without acceptance criteria; tom-grill records the ones
            # it agreed with the requirement owner as a delta. The snapshot is never
            # rewritten, so the G0 approval stays bound to the original card hash.
            if snapshot_points & grill_points:
                return "ACCEPTANCE_DELTA_CONFLICT"
            expected = snapshot_points | grill_points
            if not expected:
                return "ACCEPTANCE_CRITERIA_MISSING"
            actual = {
                item.get("acceptance_point_id") for item in content.get("traceability", [])
                if isinstance(item, dict)
            }
            return None if actual == expected else "TRACEABILITY_MISMATCH"

        if phase == "TASKS" and isinstance(predecessor_content, dict):
            expected = {
                item.get("acceptance_point_id") for item in predecessor_content.get("traceability", [])
                if isinstance(item, dict)
            }
            actual = {
                item.get("acceptance_point_id") for item in content.get("acceptance_coverage", [])
                if isinstance(item, dict)
            }
            return None if actual == expected else "TRACEABILITY_MISMATCH"

        if phase == "PLAN" and isinstance(predecessor_content, dict):
            node = next((
                item for item in predecessor_content.get("nodes", [])
                if isinstance(item, dict) and item.get("task_id") == task_id
            ), None)
            if node is None or content.get("g4_input_hash") != action.get("input_hash"):
                return "G4_INPUT_MISMATCH" if node is not None else "TASK_PLAN_MISMATCH"
            repository_revisions = {
                item.get("role"): item.get("revision")
                for item in content.get("repositories", []) if isinstance(item, dict)
            }
            if repository_revisions != action.get("source_revisions"):
                return "SOURCE_REVISION_MISMATCH"
            plan_tests = {
                item.get("test_id") for item in content.get("tests", []) if isinstance(item, dict)
            }
            plan_fixtures = set(content.get("fixtures", []))
            if (
                plan_tests != set(node.get("test_ids", []))
                or plan_fixtures != set(node.get("fixtures", []))
                or set(content.get("acceptance_point_ids", [])) != set(node.get("acceptance_point_ids", []))
            ):
                return "TASK_PLAN_MISMATCH"

        if phase == "IMPLEMENT" and isinstance(predecessor_content, dict):
            baselines = {
                item.get("role"): item.get("revision")
                for item in predecessor_content.get("repositories", []) if isinstance(item, dict)
            }
            if content.get("baseline_revisions") != baselines or baselines != action.get("baseline_revisions"):
                return "BASELINE_REVISION_MISMATCH"
            expected_tests = {
                item.get("test_id") for item in predecessor_content.get("tests", [])
                if isinstance(item, dict)
            }
            if set(content.get("test_ids", [])) != expected_tests:
                return "CHANGE_SET_MISMATCH"

        if phase == "REVIEW" and isinstance(predecessor_content, dict):
            if (
                content.get("change_set_hash") != predecessor_content.get("candidate_hash")
                or content.get("baseline_revisions") != predecessor_content.get("baseline_revisions")
            ):
                return "REVIEW_PREDECESSOR_MISMATCH"

        if phase == "DIAGNOSE" and content.get("frozen_revisions") != action.get("source_revisions"):
            return "SOURCE_REVISION_MISMATCH"
        return None

    def complete(self, run_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._complete(run_id, envelope)
        except Exception as error:
            return _failure(_exception_reason(error, "PHASE_COMPLETION_FAILED"), run_id=run_id)

    def _complete(self, run_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(envelope, dict) or envelope.get("run_id") != run_id:
            return _failure("RUN_ID_MISMATCH", run_id=run_id)
        action_id = envelope.get("action_id")
        if not isinstance(action_id, str):
            return _failure("ACTION_ID_MISMATCH", run_id=run_id)
        result_key = f"phase-completion:{run_id}:{action_id}"
        draft_hash = _canonical_hash(envelope)
        existing = self.state.idempotency_result(result_key)
        if existing is not None:
            if existing.get("draft_hash") != draft_hash:
                return _failure("COMPLETION_CONFLICT", run_id=run_id)
            return self._validated_cached_completion(run_id, existing)
        if self._blocking_pending(run_id):
            return _failure("RECOVERY_REQUIRED", run_id=run_id, retry_allowed=False)
        action = self.next(run_id)
        if not action.get("ok"):
            return self._raced_completion(run_id, result_key, draft_hash, action)
        validated = self.validate_result(action, envelope)
        if not validated.get("ok"):
            return self._raced_completion(
                run_id, result_key, draft_hash, {**validated, "run_id": run_id}
            )
        if self.knowledge is None:
            return _failure("KNOWLEDGE_SYNC_REQUIRED", run_id=run_id)
        canonical = _canonical_json(validated["draft"]["content"])
        title = _phase_title(
            action,
            _phase_attempt(self.state.events(run_id), action["phase"], action.get("task_id")),
        )
        publish_document = render_phase_markdown(
            title, validated["draft"]["content"], envelope["content_hash"], canonical
        )
        foreign = self._foreign_publish_intent(run_id, title, envelope["content_hash"])
        if foreign is not None:
            return _failure(
                "RECOVERY_REQUIRED",
                run_id=run_id,
                retry_allowed=False,
                intent_id=foreign["intent_id"],
            )
        receipt = self.knowledge.publish_phase(
            run_id,
            {
                "title": title,
                "markdown": publish_document,
                "content_hash": envelope["content_hash"],
                "canonical": canonical,
            },
            scope=workflow_spec.knowledge_scope(envelope.get("phase")),
        )
        receipt_error = self._receipt_error(run_id, envelope, receipt)
        if receipt_error is not None:
            details = {"run_id": run_id, "phase_complete": False}
            if isinstance(receipt, dict):
                if receipt.get("retry_allowed") is not None:
                    details["retry_allowed"] = receipt["retry_allowed"]
                if isinstance(receipt.get("intent_id"), str) and receipt["intent_id"]:
                    details["intent_id"] = receipt["intent_id"]
            return _failure(receipt_error, **details)
        final_envelope = {
            **validated["draft"],
            "knowledge_doc_id": receipt["child_doc_id"],
            "knowledge_url": receipt["child_url"],
            "knowledge_version": receipt["child_version"],
            # None when this phase's scope skips the iCafe comment — never the string "None".
            "icafe_comment_id": str(receipt["comment_id"]) if receipt.get("comment_id") is not None else None,
            "evidence_refs": _merge_evidence_refs(
                validated["draft"]["evidence_refs"], receipt["evidence_refs"]
            ),
        }
        stored = self.artifacts.put_envelope(final_envelope)
        if not stored.get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        recheck = self._post_publish_recheck(action, envelope, stored)
        if recheck is not None:
            return self._raced_completion(
                run_id, result_key, draft_hash, _failure(recheck, run_id=run_id)
            )
        if self._blocking_pending(run_id):
            return _failure("RECOVERY_REQUIRED", run_id=run_id, retry_allowed=False)
        target, next_task_id, completion_reason = self._completion_target(action, envelope)
        transition = self.transitions.validate(action["state"], target)
        if not transition.get("allowed"):
            return _failure(transition.get("reason_code", "INVALID_TRANSITION"), run_id=run_id)
        event_payload = {
                "previous_state": action["state"],
                "source_event_id": action["source_event_id"],
                "action_id": action_id,
                "input_hash": action["input_hash"],
                "artifact_id": stored["artifact_id"],
                "artifact_hash": envelope["content_hash"],
                "task_id": action.get("task_id"),
                **(
                    {"plan_artifact_id": stored["artifact_id"], "plan_content_hash": envelope["content_hash"]}
                    if action["phase"] == "PLAN" and target == "IMPLEMENT" else {}
                ),
                "source_revisions": validated["draft"].get("source_revisions", {}),
                "knowledge_receipt": {
                    "child_doc_id": receipt["child_doc_id"],
                    "child_version": receipt["child_version"],
                    "comment_id": str(receipt["comment_id"]),
                    "evidence_refs": receipt["evidence_refs"],
                },
                "policy_decision": transition,
                **({"task_id": next_task_id} if next_task_id is not None else {}),
            }
        result = {
            "ok": True, "reason_code": completion_reason, "phase_complete": True, "run_id": run_id,
            "state": target, "action_id": action_id,
            "artifact_id": stored["artifact_id"], "content_hash": envelope["content_hash"],
            "phase": action["phase"], "task_id": action.get("task_id"),
            "draft_hash": draft_hash, "knowledge_receipt": receipt,
        }
        committed = self.state.commit_transition_result(
            run_id, action["source_event_id"], target, event_payload, result_key, result
        )
        if committed.get("status") == "SOURCE_EVENT_MISMATCH":
            return self._raced_completion(
                run_id, result_key, draft_hash, _failure("STALE_ACTION", run_id=run_id)
            )
        if committed.get("status") == "RESULT_CONFLICT":
            return _failure("COMPLETION_CONFLICT", run_id=run_id)
        return committed["result"]

    def _raced_completion(
        self,
        run_id: str,
        result_key: str,
        draft_hash: str,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconcile a stale action to the completion that overtook it.

        Two callers completing the same action both do the work, and the loser only
        learns it lost once the winner's transition has landed -- which is the same
        transaction that stores the completion under this key, since both callers carry
        the same action id. Reporting a bare `STALE_ACTION` there says the work was
        discarded about work that did in fact complete, and the caller has no way to
        tell that from a genuinely stale draft. Staleness discovered before the publish
        is the common case: the loser is refused by `validate_result`, not by the
        commit, so checking only at the commit would miss it.
        """
        if failure.get("reason_code") != "STALE_ACTION":
            return failure
        raced = self.state.idempotency_result(result_key)
        if raced is not None and raced.get("draft_hash") == draft_hash:
            return self._validated_cached_completion(run_id, raced)
        return failure

    def _validated_cached_completion(self, run_id: str, existing: dict[str, Any]) -> dict[str, Any]:
        artifact_id = existing.get("artifact_id")
        artifact = self.artifacts.phase_artifact(artifact_id) if isinstance(artifact_id, str) else {}
        if not artifact.get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        envelope = artifact.get("envelope", {})
        if (
            envelope.get("run_id") != run_id
            or envelope.get("phase") != existing.get("phase")
            or envelope.get("task_id") != existing.get("task_id")
        ):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        receipt_error = self._receipt_error(run_id, envelope, existing.get("knowledge_receipt"))
        if receipt_error is not None:
            return _failure(receipt_error, run_id=run_id)
        events = self.state.events(run_id)
        action_key = _action_key(
            run_id, envelope.get("source_event_id"), envelope.get("phase"),
            envelope.get("task_id"), _pinned_profile_hash(events, self._repin(events)),
        )
        action = self.state.idempotency_result(action_key)
        if not isinstance(action, dict) or action.get("action_id") != envelope.get("action_id"):
            return _failure("ACTION_ID_MISMATCH", run_id=run_id)
        approval_error = self._approval_error(action, envelope)
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        collaboration_error = self._collaboration_session_error(action, envelope)
        if collaboration_error is not None:
            return _failure(collaboration_error, run_id=run_id)
        events = self.state.events(run_id)
        checkpoint = next((event for event in events if event.get("event_id") == existing.get("event_id")), None)
        payload = checkpoint.get("payload") if isinstance(checkpoint, dict) else None
        if (
            not isinstance(payload, dict)
            or checkpoint.get("state") != existing.get("state")
            or payload.get("source_event_id") != envelope.get("source_event_id")
            or payload.get("action_id") != envelope.get("action_id")
            or payload.get("artifact_id") != existing.get("artifact_id")
        ):
            return _failure("CHECKPOINT_INTEGRITY_FAILED", run_id=run_id)
        return existing

    def _post_publish_recheck(
        self, action: dict[str, Any], draft: dict[str, Any], stored: dict[str, Any]
    ) -> str | None:
        events = self.state.events(action["run_id"])
        if not events or events[-1].get("event_id") != action.get("source_event_id"):
            return "STALE_ACTION"
        profile_error = _pinned_profile_error(events, self._repin(events))
        if profile_error is not None:
            return profile_error
        predecessor = self._predecessor(
            action["run_id"],
            (_PHASES.get(action["state"]) or {}).get("predecessor"),
            action.get("task_id"),
        )
        actual_parent = predecessor.get("envelope", {}).get("content_hash") if predecessor else None
        if actual_parent != action.get("parent_artifact_hash"):
            return "PARENT_ARTIFACT_MISMATCH"
        if not self.artifacts.get(stored["artifact_id"]).get("valid"):
            return "ARTIFACT_INTEGRITY_FAILED"
        collaboration_error = self._collaboration_session_error(action, draft)
        if collaboration_error is not None:
            return collaboration_error
        approval_error = self._approval_error(action, draft)
        return approval_error

    def _task_id(self, run_id: str, state: str, current: dict[str, Any]) -> str | None:
        payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
        explicit = payload.get("task_id")
        if not isinstance(explicit, str):
            evidence = payload.get("evidence")
            explicit = evidence.get("task_id") if isinstance(evidence, dict) else None
        if isinstance(explicit, str) and explicit:
            if (
                state == "PLAN"
                and payload.get("reason_code") in {
                    "STALE_REBUILT_PLAN_RECOVERED",
                    "STALE_SUBMIT_RECOVERED",
                    "REVIEW_REFRESH_REQUIRED",
                }
                and self._task_dependencies_met(run_id, explicit)
            ):
                return explicit
            # CODE_ONLY repairs re-enter PLAN carrying the diagnosed task. That task
            # already has a passing Review — that is why SUBMIT happened — so the
            # "already reviewed" skip would plan the next DAG node instead of the
            # repair. Honour the pointer whenever PLAN was entered from DIAGNOSE.
            if (
                state == "PLAN"
                and payload.get("previous_state") == "DIAGNOSE"
                and self._task_dependencies_met(run_id, explicit)
            ):
                return explicit
            if state in {"WORKSPACE", "PLAN"} and (
                not self._task_dependencies_met(run_id, explicit)
                or self._task_reviewed(run_id, explicit)
            ):
                # A DAG amendment can give the pinned task a new prerequisite, and the
                # pointer carried by the previous event can also name a task that already
                # passed Review: after an amendment the run re-enters WORKSPACE carrying
                # whatever task the SPEC and TASKS actions happened to be bound to. Either
                # way the amended frontier is the answer, not a task that is blocked or
                # already finished.
                return self._ready_task(run_id)
            return explicit
        if state in {"WORKSPACE", "PLAN"}:
            return self._ready_task(run_id)
        if state == "IMPLEMENT":
            pinned = self._pinned_plan(run_id, current)
            if pinned is not None:
                return pinned["envelope"].get("task_id")
            artifact = self.artifacts.latest_phase(run_id, "PLAN")
            return artifact.get("envelope", {}).get("task_id") if artifact.get("valid") else None
        if state == "REVIEW":
            artifact = self.artifacts.latest_phase(run_id, "IMPLEMENT")
            return artifact.get("envelope", {}).get("task_id") if artifact.get("valid") else None
        if state == "DIAGNOSE":
            for phase in ("REVIEW", "IPIPE", "IMPLEMENT"):
                artifact = self.artifacts.latest_phase(run_id, phase)
                if artifact.get("valid"):
                    return artifact["envelope"].get("task_id")
        return None

    def _passing_reviews(self, run_id: str) -> dict[str, int]:
        """task_id -> ledger position of its newest passing Review."""
        found: dict[str, int] = {}
        for artifact in self.artifacts.phase_artifacts(run_id, "REVIEW"):
            if not artifact.get("valid") or not _passing_review(artifact["envelope"].get("content")):
                continue
            task_id = artifact["envelope"].get("task_id")
            sequence = artifact.get("sequence")
            if isinstance(task_id, str) and isinstance(sequence, int):
                found[task_id] = max(found.get(task_id, 0), sequence)
        return found

    def _task_reviewed(self, run_id: str, task_id: str) -> bool:
        """True when this task's passing Review still covers the current task DAG.

        A Review is only evidence about the scope it was written against. An amendment
        that gives an already-reviewed task new scope -- Spec 1.1.3 adding NAT64 coverage
        to T3, say -- publishes a newer task DAG, and the old Review says nothing about
        the added work. Treating it as "finished forever" emptied the frontier and parked
        the run with no task to plan.
        """
        dag = self.artifacts.latest_phase(run_id, "TASKS", None)
        dag_sequence = dag.get("sequence") if dag.get("valid") else None
        reviewed = self._passing_reviews(run_id).get(task_id)
        if reviewed is None:
            return False
        if dag_sequence is not None and reviewed <= dag_sequence:
            return False
        # Review is evidence about the IMPLEMENT candidate it consumed. Do not
        # consult a later submit descriptor here: that descriptor is created
        # after Review and would make the validity check circular (and would
        # mistake an unreviewed worktree HEAD for reviewed evidence).
        implement = self.artifacts.latest_phase(run_id, "IMPLEMENT", task_id)
        reviews = [
            artifact for artifact in self.artifacts.phase_artifacts(run_id, "REVIEW")
            if artifact.get("valid")
            and artifact.get("sequence") == reviewed
            and artifact["envelope"].get("task_id") == task_id
            and _passing_review(artifact["envelope"].get("content"))
        ]
        if not reviews:
            return False
        review = reviews[0]["envelope"]
        # Older ledger fixtures may contain a Review without the separately
        # archived IMPLEMENT envelope. Preserve their historical frontier
        # semantics; when an IMPLEMENT predecessor exists, however, require
        # the explicit hash binding below.
        if not implement.get("valid"):
            return True
        candidate_hash = implement["envelope"].get("content", {}).get("candidate_hash")
        if not candidate_hash or not review.get("content", {}).get("change_set_hash"):
            return True
        return (
            isinstance(candidate_hash, str)
            and candidate_hash
            and review.get("content", {}).get("change_set_hash") == candidate_hash
            and review.get("parent_artifact_hash")
            == implement["envelope"].get("content_hash")
        )

    def _task_dependencies_met(self, run_id: str, task_id: str) -> bool:
        """True when every DAG predecessor of task_id already has a passing Review."""
        dag = self.artifacts.latest_phase(run_id, "TASKS", None)
        if not dag.get("valid"):
            return True
        content = dag["envelope"].get("content", {})
        nodes = content.get("nodes", [])
        known = {
            node.get("task_id") for node in nodes
            if isinstance(node, dict) and isinstance(node.get("task_id"), str)
        }
        if task_id not in known:
            return True
        required = {
            edge.get("from") for edge in content.get("edges", [])
            if isinstance(edge, dict) and edge.get("to") == task_id
        }
        if not required:
            return True
        passing = {
            artifact["envelope"].get("task_id")
            for artifact in self.artifacts.phase_artifacts(run_id, "REVIEW")
            if artifact.get("valid") and _passing_review(artifact["envelope"].get("content"))
        }
        return required.issubset(passing)

    def _ready_task(self, run_id: str) -> str | None:
        dag = self.artifacts.latest_phase(run_id, "TASKS", None)
        if not dag.get("valid"):
            return None
        content = dag["envelope"].get("content", {})
        nodes = content.get("nodes", [])
        edges = content.get("edges", [])
        reviews = self._passing_reviews(run_id)
        dag_sequence = dag.get("sequence")
        # Two different questions, and answering both with one set is what parked the run
        # after an amendment. "Is this task still open?" is asked against the current DAG,
        # so a Review older than the DAG does not close it. "Are its prerequisites done?"
        # is asked about work that happened, so any passing Review counts there.
        passed_ever = set(reviews)
        finished = {
            task_id for task_id, sequence in reviews.items()
            if dag_sequence is None or sequence > dag_sequence
        }
        dependencies: dict[str, set[str]] = {
            node.get("task_id"): set() for node in nodes if isinstance(node, dict)
        }
        for edge in edges:
            if isinstance(edge, dict) and edge.get("to") in dependencies:
                dependencies[edge["to"]].add(edge.get("from"))
        for node in nodes:
            task_id = node.get("task_id") if isinstance(node, dict) else None
            if isinstance(task_id, str) and task_id not in finished and dependencies.get(task_id, set()).issubset(passed_ever):
                return task_id
        return None

    def _current_passing(self, run_id: str) -> set[str]:
        """Tasks whose passing Review still covers the current DAG."""
        return {
            task_id
            for task_id in self._passing_reviews(run_id)
            if self._task_reviewed(run_id, task_id)
        }

    def _predecessor(
        self, run_id: str, phase: str | tuple[str, ...] | None, task_id: str | None
    ) -> dict[str, Any] | None:
        if phase is None:
            return None
        if isinstance(phase, tuple):
            for artifact in reversed(self.artifacts.phase_artifacts(run_id)):
                envelope = artifact.get("envelope", {})
                if (
                    artifact.get("valid")
                    and envelope.get("phase") in phase
                    and (task_id is None or envelope.get("task_id") in {None, task_id})
                ):
                    return artifact
            return None
        if phase == "PLAN":
            events = self.state.events(run_id)
            current = events[-1] if events else {}
            pinned = self._pinned_plan(run_id, current)
            if pinned is not None:
                if task_id is None or pinned["envelope"].get("task_id") == task_id:
                    return pinned
                return None
        exact_task = task_id if phase in {"PLAN", "IMPLEMENT", "REVIEW"} else None
        artifact = self.artifacts.latest_phase(run_id, phase, exact_task)
        return artifact if artifact.get("valid") else None

    def _pinned_plan(self, run_id: str, current: dict[str, Any]) -> dict[str, Any] | None:
        """Return IMPLEMENT's explicitly pinned Plan, refusing stale or forged pins."""
        payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
        artifact_id = payload.get("plan_artifact_id")
        content_hash = payload.get("plan_content_hash")
        if not isinstance(artifact_id, str) or not isinstance(content_hash, str):
            return None
        artifact = self.artifacts.phase_artifact(artifact_id)
        envelope = artifact.get("envelope") if artifact.get("valid") else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("run_id") != run_id
            or envelope.get("phase") != "PLAN"
            or envelope.get("content_hash") != content_hash
        ):
            return None
        return artifact

    def _source_revisions(
        self, current: dict[str, Any], predecessor: dict[str, Any] | None
    ) -> dict[str, str]:
        payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
        for key in ("source_revisions", "repo_revisions"):
            value = payload.get(key)
            if isinstance(value, dict):
                return dict(value)
        plan_artifact_id = payload.get("plan_artifact_id")
        if isinstance(plan_artifact_id, str):
            plan = self.artifacts.phase_artifact(plan_artifact_id)
            envelope = plan.get("envelope") if plan.get("valid") else None
            value = envelope.get("source_revisions") if isinstance(envelope, dict) else None
            if isinstance(value, dict):
                return dict(value)
        if predecessor is not None:
            value = predecessor["envelope"].get("source_revisions")
            if isinstance(value, dict):
                return dict(value)
        return {}

    def _approval_error(self, action: dict[str, Any], result: dict[str, Any]) -> str | None:
        gate = action.get("required_human_gate")
        if gate is None:
            return None if result.get("approval_id") is None else "APPROVAL_MISMATCH"
        approval_id = result.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            return "APPROVAL_REQUIRED"
        if self.approvals is None:
            return "APPROVAL_REQUIRED"
        approval = self.approvals.get(approval_id)
        if not isinstance(approval, dict):
            return "APPROVAL_REQUIRED"
        if approval.get("run_id") != action.get("run_id") or approval.get("run_id") == "legacy":
            return "APPROVAL_RUN_MISMATCH"
        if approval.get("action") != gate:
            return "APPROVAL_GATE_MISMATCH"
        approval_input_hash = result.get("approval_input_hash")
        if approval.get("input_hash") != approval_input_hash:
            return "APPROVAL_INPUT_MISMATCH"
        if approval.get("effective_decision") != "APPROVE":
            return "APPROVAL_REQUIRED"
        gate_check = self.evidence_gate.check(
            gate,
            {
                "run_id": action["run_id"], "input_hash": approval_input_hash,
                "approved_input_hash": approval["input_hash"], "approval_id": approval_id,
                "approval_record": approval,
            },
        )
        return None if gate_check.get("passed") else gate_check.get("reason_code", "APPROVAL_REQUIRED")

    def _receipt_error(self, run_id: str, envelope: dict[str, Any], receipt: Any) -> str | None:
        if not isinstance(receipt, dict) or not receipt.get("ok"):
            if isinstance(receipt, dict) and receipt.get("reason_code") in {"QUERY_REQUIRED", "RECOVERY_REQUIRED"}:
                return "RECOVERY_REQUIRED"
            # Keep the publisher's own reason when it named one. Collapsing every
            # failed publish into KNOWLEDGE_PUBLISH_INCOMPLETE hid
            # KU_CHILD_CONTENT_UNSETTLED / KU_IMMUTABLE_CONFLICT on the BGW-1956
            # T3 PLAN and IMPLEMENT retries, so the operator had to call
            # publish_phase by hand to see what actually happened.
            if isinstance(receipt, dict) and isinstance(receipt.get("reason_code"), str) and receipt["reason_code"]:
                return receipt["reason_code"]
            return "KNOWLEDGE_PUBLISH_INCOMPLETE"
        if receipt.get("run_id", run_id) != run_id or receipt.get("artifact_hash") != envelope["content_hash"]:
            return "KU_RECEIPT_MISMATCH"
        # Slimming Stage B: only require the writes this phase's scope actually performs.
        scope = workflow_spec.knowledge_scope(envelope.get("phase"))
        wants_ku = scope in ("both", "ku_only")
        wants_icafe = scope in ("both", "icafe_only")
        try:
            refs = validate_evidence_refs(receipt.get("evidence_refs"))
        except ValueError:
            return "KNOWLEDGE_RECEIPT_INVALID"
        if wants_ku:
            if not receipt.get("child_doc_id") or not receipt.get("child_version"):
                return "KU_RECEIPT_MISMATCH"
            if not _canonical_ku_url(receipt.get("child_url"), receipt.get("child_doc_id")):
                return "KU_RECEIPT_MISMATCH"
            if f"ku:{receipt['child_doc_id']}/{receipt['child_version']}" not in refs:
                return "KU_RECEIPT_MISMATCH"
        if wants_icafe:
            comment_id = receipt.get("comment_id")
            if comment_id is None or str(comment_id) == "":
                return "ICAFE_RECEIPT_MISMATCH"
            card_id = _requirement_id(self.state.events(run_id))
            if f"icafe:{card_id}/{comment_id}" not in refs:
                return "ICAFE_RECEIPT_MISMATCH"
        return None


    def _completion_target(
        self, action: dict[str, Any], envelope: dict[str, Any]
    ) -> tuple[str, str | None, str]:
        if action["phase"] == "REVIEW":
            review = envelope["content"]
            if review.get("verdict") == "INCOMPLETE" or review.get("completeness_state") != "COMPLETE":
                return "STOPPED", None, "REVIEW_INCOMPLETE"
            findings = review.get("findings")
            findings = findings if isinstance(findings, list) else []
            # An unclarified finding is a question, and `tom-diagnose` root-causes
            # failures rather than answering questions: sending it there would ask the
            # diagnosis phase to invent the verification the reviewer could not do.
            if any(
                isinstance(finding, dict)
                and finding.get("classification") == "NEEDS_CLARIFICATION"
                for finding in findings
            ):
                return "STOPPED", None, "REVIEW_NEEDS_CLARIFICATION"
            if not _passing_review(review):
                return "DIAGNOSE", action.get("task_id"), "OK"
            # One frontier at a time: the reviewed Change Set still has to reach
            # iCode. Jumping to the next DAG node from here left BGW-1956 T3
            # unsubmitted while next() asked for T1's G4.
            return "SUBMIT", action.get("task_id"), "OK"
        if action["phase"] == "DIAGNOSE":
            content = envelope["content"]
            route = content.get("route")
            if route == "DIAGNOSIS_INCOMPLETE":
                return "STOPPED", None, "DIAGNOSIS_INCOMPLETE"
            if route != "REPAIR":
                return route, None, "OK"
            # Deterministic cross-run guard (MEDIUM-004): even when the diagnosis proposes
            # yet another REPAIR, a root cause the FailureCase library has already seen
            # unresolved across >= CROSS_RUN_RECURRENCE_THRESHOLD runs is escalated to
            # architecture review instead of blindly repaired again. The signature is read
            # from the diagnosis the producer just filed, never reconstructed from events.
            escalation = self._cross_run_escalation(content.get("failure_signature"))
            if escalation is not None:
                return escalation
            # A code-only repair leaves the Spec and the DAG standing, so it re-enters
            # at PLAN and wins a fresh G4 there. Anything that amends the Spec or moves
            # task scope re-enters at SPEC, which is also the default when the diagnosis
            # does not say -- the conservative reading of an older artifact.
            if content.get("repair_scope") == "CODE_ONLY":
                return "PLAN", action.get("task_id"), "OK"
            return "SPEC", None, "OK"
        target = action.get("target_state")
        if not isinstance(target, str):
            raise ValueError("COMPLETION_TARGET_REQUIRED")
        return target, None, "OK"

    def _cross_run_escalation(
        self, signature: Any
    ) -> tuple[str, None, str] | None:
        """Escalate to ARCHITECTURE_REVIEW when the FailureCase library says this root cause
        keeps recurring unresolved across runs; None to let the per-run repair policy stand."""
        if not isinstance(signature, str) or not signature:
            return None
        try:
            case = self.state.failure_case(signature)
        except Exception:
            _log.warning(
                "failure-case read failed for signature %s (best-effort)",
                signature, exc_info=True,
            )
            return None
        verdict = repair_policy.known_failure_verdict(case)
        if verdict is None:
            return None
        return "ARCHITECTURE_REVIEW", None, verdict["reason_code"]

    def _record_cross_run_failure(self, run_id: str, content: dict[str, Any]) -> None:
        """Record one occurrence of a runtime failure's signature in the cross-run FailureCase
        library. Retained for callers that record outside a transition commit; the IPIPE->
        DIAGNOSE path now records atomically via commit_transition_result's failure_accounting
        (R-M6). Best-effort: the library must never fail the transition it observes."""
        signature = content.get("failure_signature")
        if not isinstance(signature, str) or not signature:
            return
        try:
            self.state.record_failure_case(
                signature, content.get("classification"), run_id, resolved=False
            )
        except Exception:
            _log.warning(
                "failure-case write failed for run %s signature %s (best-effort)",
                run_id, signature, exc_info=True,
            )

    def _ready_task_excluding(self, run_id: str, completing_task: str | None) -> str | None:
        """Next open DAG node, treating `completing_task` as finished.

        "Finished" is a Review that still covers the current DAG. Counting any
        historical pass emptied the frontier after an amendment — BGW-1956 T0's
        Review then went to SUBMIT while T1/T2 Reviews predated the DAG and T3
        was REJECT, and IPIPE started over that half-finished set.
        """
        dag = self.artifacts.latest_phase(run_id, "TASKS", None)
        if not dag.get("valid"):
            return None
        content = dag["envelope"].get("content", {})
        nodes = content.get("nodes", [])
        edges = content.get("edges", [])
        reviews = self._passing_reviews(run_id)
        dag_sequence = dag.get("sequence")
        passed_ever = set(reviews)
        finished = {
            task_id for task_id, sequence in reviews.items()
            if dag_sequence is None or sequence > dag_sequence
        }
        if completing_task:
            passed_ever.add(completing_task)
            finished.add(completing_task)
        dependencies: dict[str, set[str]] = {
            node.get("task_id"): set() for node in nodes if isinstance(node, dict)
        }
        for edge in edges:
            if isinstance(edge, dict) and edge.get("to") in dependencies:
                dependencies[edge["to"]].add(edge.get("from"))
        for node in nodes:
            task_id = node.get("task_id") if isinstance(node, dict) else None
            if isinstance(task_id, str) and task_id not in finished and dependencies.get(task_id, set()).issubset(passed_ever):
                return task_id
        return None

    def ingest_ipipe_evidence(self, run_id: str, content: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._ingest_ipipe_evidence(run_id, content)
        except Exception as error:
            return _failure(_exception_reason(error, "IPIPE_EVIDENCE_INGEST_FAILED"), run_id=run_id)

    def _ingest_ipipe_evidence(self, run_id: str, content: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(content, dict) or not isinstance(content.get("build_id"), str):
            return _failure("INVALID_INPUT", run_id=run_id)
        content_hash = _canonical_hash(content)
        result_key = f"ipipe-ingest:{run_id}:{content['build_id']}"
        existing = self.state.idempotency_result(result_key)
        if existing is not None:
            if existing.get("ingest_hash") != content_hash:
                return _failure("COMPLETION_CONFLICT", run_id=run_id)
            return self._validated_cached_ipipe_ingestion(run_id, existing)
        events = self.state.events(run_id)
        if events and events[-1].get("state") != "IPIPE":
            prior = self.artifacts.latest_phase(run_id, "IPIPE", None)
            if prior.get("valid"):
                return _failure("COMPLETION_CONFLICT", run_id=run_id)
        action = self.next(run_id)
        if not action.get("ok"):
            return action
        if action.get("state") != "IPIPE" or action.get("controller") != "ipipe":
            return _failure("INVALID_STATE", run_id=run_id)
        issues = validate_named_schema(content, "ipipe-evidence")
        if issues:
            return {**_failure("SCHEMA_INVALID", run_id=run_id), "schema_errors": [
                {"path": issue.path, "kind": issue.kind} for issue in issues
            ]}
        events = self.state.events(run_id)
        payload = events[-1]["payload"]
        binding_error = self._ipipe_binding_error(events, payload, content)
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        profile = load_profile(payload["profile_path"], check_paths=False).get("profile")
        expected_pipeline = _registered_pipeline(
            profile.get("pipeline_profile") if isinstance(profile, dict) else {},
            content.get("module"),
        )
        target_submission = self._evidence_submission(
            payload, content.get("module"), expected_pipeline, events
        )
        target_artifact = (
            self.artifacts.get(target_submission.get("artifact_id"))
            if isinstance(target_submission, dict)
            and isinstance(target_submission.get("artifact_id"), str)
            else {"valid": False}
        )
        if (
            not isinstance(target_submission, dict)
            or not target_artifact.get("valid")
            or target_artifact.get("sha256") != target_submission.get("sha256")
        ):
            return _failure("PREDECESSOR_REQUIRED", run_id=run_id)
        revisions = target_submission["source_revisions"]
        approval_error = self._controller_approval_error(run_id, payload, "G7")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        # Each required module's evidence is its own artifact. phase_artifacts.action_id is
        # UNIQUE, so a multi-module run must not reuse the single IPIPE action_id for every
        # module (that collided as ARTIFACT_CONFLICT on the 2nd module — HIGH-003); the
        # stored identity is scoped by module while the transition still rides the IPIPE
        # source event. Replaying the same module is stable (same hash), so it stays idempotent.
        artifact_action_id = _canonical_hash({
            "ipipe_action_id": action["action_id"], "module": content.get("module")})
        draft = {
            "action_id": artifact_action_id, "source_event_id": action["source_event_id"],
            "host": "comate", "run_id": run_id, "phase": "IPIPE", "task_id": None,
            "schema_version": "1", "input_hash": action["input_hash"], "content_hash": content_hash,
            "source_revisions": revisions, "parent_artifact_hash": target_submission["sha256"],
            "knowledge_doc_id": None, "knowledge_url": None, "knowledge_version": None,
            "icafe_comment_id": None, "evidence_refs": content.get("remote_evidence_refs", []),
            "approval_id": payload.get("approval_id"),
            "approval_input_hash": payload.get("approval_input_hash"), "content": content,
        }
        ipipe_title = f"08-ipipe-evidence/{content['build_id']}"
        ipipe_canonical = _canonical_json(content)
        receipt = self.knowledge.publish_phase(run_id, {
            "title": ipipe_title,
            "markdown": render_phase_markdown(
                ipipe_title, content, content_hash, ipipe_canonical
            ),
            "content_hash": content_hash,
            "canonical": ipipe_canonical,
        }, scope=workflow_spec.knowledge_scope("IPIPE"))
        receipt_error = self._receipt_error(run_id, draft, receipt)
        if receipt_error:
            return _failure(receipt_error, run_id=run_id)
        final = {
            **draft, "knowledge_doc_id": receipt["child_doc_id"], "knowledge_url": receipt["child_url"],
            "knowledge_version": receipt["child_version"], "icafe_comment_id": str(receipt["comment_id"]),
            "evidence_refs": _merge_evidence_refs(
                draft["evidence_refs"], receipt["evidence_refs"]
            ),
        }
        stored = self.artifacts.put_envelope(final)
        events = self.state.events(run_id)
        if not events or events[-1].get("event_id") != action["source_event_id"]:
            raced = self.state.idempotency_result(result_key)
            return self._validated_cached_ipipe_ingestion(run_id, raced) if raced else _failure("STALE_ACTION", run_id=run_id)
        profile_error = _pinned_profile_error(events, self._repin(events))
        if profile_error is not None:
            return _failure(profile_error, run_id=run_id)
        binding_error = self._ipipe_binding_error(events, payload, content)
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        current_target = self._evidence_submission(
            payload, content.get("module"), expected_pipeline, events
        )
        current_submission = (
            self.artifacts.get(current_target.get("artifact_id"))
            if isinstance(current_target, dict)
            and isinstance(current_target.get("artifact_id"), str)
            else {"valid": False}
        )
        if (
            not current_submission.get("valid")
            or not isinstance(current_target, dict)
            or current_submission.get("sha256") != current_target.get("sha256")
            or current_submission.get("sha256") != final["parent_artifact_hash"]
        ):
            return _failure("PREDECESSOR_REQUIRED", run_id=run_id)
        approval_error = self._controller_approval_error(run_id, payload, "G7")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        if not self.artifacts.get(stored["artifact_id"]).get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        outstanding = self._ipipe_outstanding_modules(events, content)
        if outstanding:
            # Every required pipeline has to report before the run may leave IPIPE. The
            # evidence just stored stays, so the next module's ingestion carries on from
            # here and the last one performs the transition. Leaving on the first success
            # would release a change whose other half was never built.
            return _failure(
                "PIPELINE_EVIDENCE_INCOMPLETE", run_id=run_id,
                artifact_id=stored["artifact_id"], outstanding_modules=outstanding,
            )
        target = {"SUCCESS": "RELEASE", "FAILURE": "DIAGNOSE", "BLOCKED": "ENVIRONMENT_BLOCKED"}[content["status"]]
        transition = self.transitions.validate("IPIPE", target)
        if not transition.get("allowed"):
            return _failure(transition.get("reason_code", "INVALID_TRANSITION"), run_id=run_id)
        result = {
            "ok": True, "reason_code": "OK", "phase_complete": True, "action_id": artifact_action_id,
            "artifact_id": stored["artifact_id"], "content_hash": content_hash, "phase": "IPIPE",
            "task_id": None, "draft_hash": _canonical_hash(draft), "ingest_hash": content_hash,
            "knowledge_receipt": receipt,
        }
        # Fold the cross-run FailureCase write into the SAME transaction as the IPIPE->DIAGNOSE
        # transition (R-M6), so a crash or a replay between commit and a best-effort write can
        # never leave a real failure unrecorded. Recorded only on the first COMMIT.
        failure_accounting = None
        signature = content.get("failure_signature") if isinstance(content, dict) else None
        if target == "DIAGNOSE" and isinstance(signature, str) and signature:
            failure_accounting = {"record": {
                "signature": signature, "classification": content.get("classification"),
                "run_id": run_id,
            }}
        committed = self.state.commit_transition_result(
            run_id, action["source_event_id"], target,
            {"previous_state": "IPIPE", "artifact_id": stored["artifact_id"],
             "action_id": action["action_id"], "source_event_id": action["source_event_id"],
             "policy_decision": transition},
            result_key, result, failure_accounting=failure_accounting,
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return _failure("COMPLETION_CONFLICT", run_id=run_id)
        raced = self.state.idempotency_result(result_key)
        return self._validated_cached_ipipe_ingestion(run_id, raced) if raced else _failure("STALE_ACTION", run_id=run_id)

    def ingest_release_evidence(
        self, run_id: str, content: dict[str, Any], approval: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return self._ingest_release_evidence(run_id, content, approval)
        except Exception as error:
            return _failure(_exception_reason(error, "RELEASE_EVIDENCE_INGEST_FAILED"), run_id=run_id)

    def _release_binding_error(self, run_id: str, content: Any) -> str | None:
        """The release evidence has to describe the exact build the pipeline proved.

        Binding it to the IPIPE predecessor's successful evidence — same pipeline, module,
        revisions, release rule, environment and build — means a release can only be
        recorded for the change the pipeline actually passed, never a different or later
        build, and never before a passing pipeline exists.
        """
        predecessor = self._predecessor(run_id, "IPIPE", None)
        if predecessor is None:
            return "PREDECESSOR_REQUIRED"
        ipipe = predecessor.get("envelope", {}).get("content")
        if not isinstance(ipipe, dict) or ipipe.get("status") != "SUCCESS":
            return "PREDECESSOR_REQUIRED"
        if not isinstance(content, dict):
            return "SCHEMA_INVALID"
        if content.get("build_id") != ipipe.get("build_id"):
            return "BUILD_IDENTITY_MISMATCH"
        if content.get("pipeline_id") != ipipe.get("pipeline_id") or content.get("module") != ipipe.get("module"):
            return "PIPELINE_IDENTITY_MISMATCH"
        if content.get("revisions") != ipipe.get("revisions"):
            return "SOURCE_REVISION_MISMATCH"
        if content.get("release_rule") != ipipe.get("release_rule"):
            return "RELEASE_RULE_MISMATCH"
        if content.get("environment_fingerprint") != ipipe.get("environment_fingerprint"):
            return "ENV_FINGERPRINT_MISMATCH"
        return None

    def _validated_cached_release_ingestion(
        self, run_id: str, existing: Any
    ) -> dict[str, Any]:
        if not isinstance(existing, dict):
            return _failure("STALE_ACTION", run_id=run_id)
        artifact_id = existing.get("artifact_id")
        artifact = self.artifacts.phase_artifact(artifact_id) if isinstance(artifact_id, str) else {}
        if not artifact.get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        envelope = artifact["envelope"]
        if (envelope.get("run_id") != run_id or envelope.get("phase") != "RELEASE"
                or envelope.get("task_id") is not None):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        receipt_error = self._receipt_error(run_id, envelope, existing.get("knowledge_receipt"))
        if receipt_error is not None:
            return _failure(receipt_error, run_id=run_id)
        binding_error = self._release_binding_error(run_id, envelope.get("content"))
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        approval_error = self._controller_approval_error(run_id, {
            "approval_id": envelope.get("approval_id"),
            "approval_input_hash": envelope.get("approval_input_hash"),
        }, "G9")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        return existing

    def _ingest_release_evidence(
        self, run_id: str, content: dict[str, Any], approval: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(content, dict) or not isinstance(content.get("build_id"), str):
            return _failure("INVALID_INPUT", run_id=run_id)
        if not isinstance(approval, dict) or not isinstance(approval.get("approval_id"), str):
            return _failure("APPROVAL_REQUIRED", run_id=run_id)
        content_hash = _canonical_hash(content)
        result_key = f"release-ingest:{run_id}:{content['build_id']}"
        existing = self.state.idempotency_result(result_key)
        if existing is not None:
            if existing.get("ingest_hash") != content_hash:
                return _failure("COMPLETION_CONFLICT", run_id=run_id)
            return self._validated_cached_release_ingestion(run_id, existing)
        events = self.state.events(run_id)
        if events and events[-1].get("state") != "RELEASE":
            prior = self.artifacts.latest_phase(run_id, "RELEASE", None)
            if prior.get("valid"):
                return _failure("COMPLETION_CONFLICT", run_id=run_id)
        action = self.next(run_id)
        if not action.get("ok"):
            return action
        if action.get("state") != "RELEASE" or action.get("controller") != "release":
            return _failure("INVALID_STATE", run_id=run_id)
        issues = validate_named_schema(content, "release-evidence")
        if issues:
            return {**_failure("SCHEMA_INVALID", run_id=run_id), "schema_errors": [
                {"path": issue.path, "kind": issue.kind} for issue in issues
            ]}
        # G9 authorizes this exact release action; the operator approves it while the run
        # is already at RELEASE, so — unlike IPIPE's G7 carried in the payload — the hash is
        # the release action's own input_hash, checked against the ledger here.
        if approval.get("input_hash") != action.get("input_hash"):
            return _failure("APPROVAL_INPUT_MISMATCH", run_id=run_id)
        approval_check = {
            "approval_id": approval.get("approval_id"),
            "approval_input_hash": approval.get("input_hash"),
        }
        approval_error = self._controller_approval_error(run_id, approval_check, "G9")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        binding_error = self._release_binding_error(run_id, content)
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        predecessor = self._predecessor(run_id, "IPIPE", None)
        parent_hash = predecessor["envelope"]["content_hash"]
        revisions = predecessor["envelope"]["content"]["revisions"]
        draft = {
            "action_id": action["action_id"], "source_event_id": action["source_event_id"],
            "host": "comate", "run_id": run_id, "phase": "RELEASE", "task_id": None,
            "schema_version": "1", "input_hash": action["input_hash"], "content_hash": content_hash,
            "source_revisions": revisions, "parent_artifact_hash": parent_hash,
            "knowledge_doc_id": None, "knowledge_url": None, "knowledge_version": None,
            "icafe_comment_id": None, "evidence_refs": content.get("remote_evidence_refs", []),
            "approval_id": approval.get("approval_id"),
            "approval_input_hash": approval.get("input_hash"), "content": content,
        }
        # __RELEASE_INGEST_TAIL__
        title = f"09-release-evidence/{content['build_id']}"
        canonical = _canonical_json(content)
        receipt = self.knowledge.publish_phase(run_id, {
            "title": title,
            "markdown": render_phase_markdown(title, content, content_hash, canonical),
            "content_hash": content_hash, "canonical": canonical,
        }, scope=workflow_spec.knowledge_scope("RELEASE"))
        receipt_error = self._receipt_error(run_id, draft, receipt)
        if receipt_error:
            return _failure(receipt_error, run_id=run_id)
        final = {
            **draft, "knowledge_doc_id": receipt["child_doc_id"], "knowledge_url": receipt["child_url"],
            "knowledge_version": receipt["child_version"], "icafe_comment_id": str(receipt["comment_id"]),
            "evidence_refs": _merge_evidence_refs(draft["evidence_refs"], receipt["evidence_refs"]),
        }
        stored = self.artifacts.put_envelope(final)
        events = self.state.events(run_id)
        if not events or events[-1].get("event_id") != action["source_event_id"]:
            raced = self.state.idempotency_result(result_key)
            if raced:
                return self._validated_cached_release_ingestion(run_id, raced)
            return _failure("STALE_ACTION", run_id=run_id)
        binding_error = self._release_binding_error(run_id, content)
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        approval_error = self._controller_approval_error(run_id, approval_check, "G9")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        if not self.artifacts.get(stored["artifact_id"]).get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        transition = self.transitions.validate("RELEASE", "RELEASE_SUCCESS")
        if not transition.get("allowed"):
            return _failure(transition.get("reason_code", "INVALID_TRANSITION"), run_id=run_id)
        result = {
            "ok": True, "reason_code": "OK", "phase_complete": True, "action_id": action["action_id"],
            "artifact_id": stored["artifact_id"], "content_hash": content_hash, "phase": "RELEASE",
            "task_id": None, "draft_hash": _canonical_hash(draft), "ingest_hash": content_hash,
            "knowledge_receipt": receipt,
        }
        committed = self.state.commit_transition_result(
            run_id, action["source_event_id"], "RELEASE_SUCCESS",
            {"previous_state": "RELEASE", "artifact_id": stored["artifact_id"],
             "action_id": action["action_id"], "source_event_id": action["source_event_id"],
             "policy_decision": transition},
            result_key, result, failure_accounting={"resolve_run": run_id},
        )
        if committed.get("status") in {"COMMITTED", "REPLAY"}:
            return committed["result"]
        if committed.get("status") == "RESULT_CONFLICT":
            return _failure("COMPLETION_CONFLICT", run_id=run_id)
        raced = self.state.idempotency_result(result_key)
        if raced:
            return self._validated_cached_release_ingestion(run_id, raced)
        return _failure("STALE_ACTION", run_id=run_id)

    def _controller_approval_error(
        self, run_id: str, payload: dict[str, Any], gate: str
    ) -> str | None:
        approval_id = payload.get("approval_id")
        approval_hash = payload.get("approval_input_hash")
        if not isinstance(approval_id, str) or not approval_id or self.approvals is None:
            return "APPROVAL_REQUIRED"
        approval = self.approvals.get(approval_id)
        if not isinstance(approval, dict):
            return "APPROVAL_REQUIRED"
        if approval.get("run_id") != run_id or approval.get("run_id") == "legacy":
            return "APPROVAL_RUN_MISMATCH"
        if approval.get("action") != gate:
            return "APPROVAL_GATE_MISMATCH"
        if approval.get("input_hash") != approval_hash:
            return "APPROVAL_INPUT_MISMATCH"
        return None if approval.get("effective_decision") == "APPROVE" else "APPROVAL_REQUIRED"

    def _ipipe_required_modules(self, events: list[dict[str, Any]]) -> list[str]:
        """The run's required-for-release modules, from its pinned profile."""
        intake = events[0].get("payload") if events else None
        profile_path = intake.get("profile_path") if isinstance(intake, dict) else None
        if not isinstance(profile_path, str) or not profile_path:
            return []
        loaded = load_profile(profile_path, check_paths=False)
        profile = loaded.get("profile") if loaded.get("ready") else None
        if not isinstance(profile, dict):
            return []
        return _required_modules(profile.get("pipeline_profile"))

    def _ipipe_passed_modules(self, events: list[dict[str, Any]]) -> set[str]:
        """Required modules whose archived SUCCESS evidence matches their CURRENT submission
        binding — pipeline / release-rule / environment / revision (R-H2).

        Binding-aware, not status-only: a success recorded against an old revision (later
        superseded by a repair that changed the revision) no longer counts, so the worker's
        "next module" decision matches this completion judgment instead of skipping a module
        whose current code was never actually built."""
        required = set(self._ipipe_required_modules(events))
        if not required:
            return set()
        run_id = events[0].get("run_id") if events else None
        payload = events[-1].get("payload") if events else None
        if not run_id or not isinstance(payload, dict):
            return set()
        intake = events[0].get("payload") if events else None
        profile_path = intake.get("profile_path") if isinstance(intake, dict) else None
        loaded = load_profile(profile_path, check_paths=False) if isinstance(profile_path, str) else {}
        profile = loaded.get("profile") if loaded.get("ready") else None
        pipeline = profile.get("pipeline_profile") if isinstance(profile, dict) else None
        environment = profile.get("environment_profile") if isinstance(profile, dict) else None
        expected_environment = _canonical_hash(environment)
        passed: set[str] = set()
        for artifact in self.artifacts.phase_artifacts(run_id, "IPIPE"):
            if not artifact.get("valid"):
                continue
            envelope = artifact.get("envelope")
            stored = envelope.get("content") if isinstance(envelope, dict) else None
            if not isinstance(stored, dict) or stored.get("status") != "SUCCESS":
                continue
            module = stored.get("module")
            if module not in required:
                continue
            expected_pipeline = _registered_pipeline(pipeline, module)
            expected_release_rule = _registered_release_rule(pipeline, module)
            target = self._evidence_submission(payload, module, expected_pipeline, events)
            if not isinstance(target, dict):
                continue
            if (
                envelope.get("parent_artifact_hash") != target.get("sha256")
                or stored.get("pipeline_id") != expected_pipeline
                or stored.get("release_rule") != expected_release_rule
                or stored.get("environment_fingerprint") != expected_environment
                or stored.get("revisions") != target.get("source_revisions")
            ):
                continue
            passed.add(str(module))
        return passed

    def ipipe_outstanding_modules(self, run_id: str) -> list[str]:
        """Required modules still lacking a current-binding SUCCESS, computed from the run's
        archived evidence (R-H2). Empty when there are no required modules or all have passed.
        The worker calls this so its module scheduling shares the protocol's binding-aware
        completion judgment rather than a status-only over-approximation."""
        events = self.state.events(run_id)
        required = self._ipipe_required_modules(events)
        if not required:
            return []
        passed = self._ipipe_passed_modules(events)
        return [module for module in required if module not in passed]

    def _ipipe_outstanding_modules(
        self, events: list[dict[str, Any]], content: dict[str, Any]
    ) -> list[str]:
        """Required modules with no passing pipeline evidence yet.

        Only a success can wait: a failure or a blocked environment is the answer for
        the whole run, and holding it back to ask the remaining pipelines would delay
        the diagnosis without changing it.
        """
        if content.get("status") != "SUCCESS":
            return []
        required = self._ipipe_required_modules(events)
        if not required:
            return []
        passed = self._ipipe_passed_modules(events)
        # The just-stored current module counts even if the archived read raced it.
        passed.add(str(content.get("module")))
        return [module for module in required if module not in passed]

    def _ipipe_binding_error(
        self, events: list[dict[str, Any]], payload: Any, content: Any = None
    ) -> str | None:
        profile_error = _pinned_profile_error(events, self._repin(events))
        if profile_error is not None:
            return profile_error
        if not isinstance(payload, dict):
            return "PROJECT_NOT_READY"
        profile_path = payload.get("profile_path")
        if not isinstance(profile_path, str) or not profile_path:
            return "PROJECT_NOT_READY"
        loaded = load_profile(profile_path, check_paths=False)
        if not loaded.get("ready"):
            return "PROJECT_NOT_READY"
        profile = loaded.get("profile")
        if not isinstance(profile, dict) or profile.get("project_id") != payload.get("project"):
            return "PROJECT_NOT_READY"
        pipeline = profile.get("pipeline_profile")
        repositories = profile.get("business_repos")
        if not isinstance(pipeline, dict) or not isinstance(repositories, list):
            return "PROJECT_NOT_READY"
        modules = {
            repository.get("module") for repository in repositories
            if isinstance(repository, dict) and isinstance(repository.get("module"), str)
        }
        payload_module = payload.get("module")
        expected_release_rule = _registered_release_rule(pipeline, payload_module)
        # A cross-repository requirement has one pipeline per module, so identity is
        # checked against the pipeline registered for *this* module. The single
        # `pipeline_id` remains the answer for a profile that registers none.
        if payload.get("pipeline_id") != _registered_pipeline(pipeline, payload_module):
            return "PIPELINE_IDENTITY_MISMATCH"
        if payload_module not in modules:
            return "PIPELINE_IDENTITY_MISMATCH"
        if payload.get("release_rule") != expected_release_rule:
            return "RELEASE_RULE_MISMATCH"
        expected_environment = _canonical_hash(profile.get("environment_profile"))
        if payload.get("environment_fingerprint") != expected_environment:
            return "ENV_FINGERPRINT_MISMATCH"
        if not _valid_revisions(payload.get("source_revisions")):
            return "SOURCE_REVISION_REQUIRED"
        # The payload names the primary submission, but every required module needs its
        # own pipeline evidence, so evidence about another module is checked against the
        # submission recorded for *that* module rather than the primary's.
        reported = content.get("module") if isinstance(content, dict) else None
        module = reported if isinstance(reported, str) else payload_module
        if module not in modules:
            return "PIPELINE_IDENTITY_MISMATCH"
        expected_pipeline = _registered_pipeline(pipeline, module)
        expected_release_rule = _registered_release_rule(pipeline, module)
        target = self._evidence_submission(payload, module, expected_pipeline, events)
        if target is None:
            return "PIPELINE_IDENTITY_MISMATCH"
        revisions = target["source_revisions"]
        if not _valid_revisions(revisions):
            return "SOURCE_REVISION_REQUIRED"
        artifact_id = target["artifact_id"]
        submission = self.artifacts.get(artifact_id) if isinstance(artifact_id, str) else {"valid": False}
        if not submission.get("valid") or submission.get("sha256") != target["sha256"]:
            return "PREDECESSOR_REQUIRED"
        expected_submission_binding = {
            "pipeline_id": expected_pipeline,
            "module": module,
            "release_rule": expected_release_rule,
            "source_revisions": revisions,
            "environment_fingerprint": expected_environment,
        }
        if submission.get("metadata", {}).get("controller_binding") != expected_submission_binding:
            return "SUBMISSION_BINDING_MISMATCH"
        if content is not None:
            if not isinstance(content, dict):
                return "SCHEMA_INVALID"
            if content.get("pipeline_id") != expected_pipeline or content.get("module") != module:
                return "PIPELINE_IDENTITY_MISMATCH"
            if content.get("release_rule") != expected_release_rule:
                return "RELEASE_RULE_MISMATCH"
            if content.get("revisions") != revisions:
                return "SOURCE_REVISION_MISMATCH"
            if content.get("environment_fingerprint") != expected_environment:
                return "ENV_FINGERPRINT_MISMATCH"
        return None

    def _evidence_submission(
        self,
        payload: dict[str, Any],
        module: str,
        expected_pipeline: Any,
        events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The submission the evidence is about, keyed by the module it reports."""
        if module == payload.get("module"):
            return {
                "artifact_id": payload.get("submission_artifact_id"),
                "sha256": payload.get("submission_hash"),
                "source_revisions": payload.get("source_revisions"),
            }
        for entry in payload.get("submissions") or []:
            if not isinstance(entry, dict):
                continue
            binding = entry.get("controller_binding")
            if isinstance(binding, dict) and binding.get("module") == module:
                return {
                    "artifact_id": entry.get("artifact_id"),
                    "sha256": entry.get("sha256"),
                    "source_revisions": binding.get("source_revisions"),
                }
        # The payload's list is a snapshot taken at the transition. A submission
        # archived for this module afterwards is still a submission of this run, and
        # the caller re-checks the whole binding, so the ledger stays the authority.
        run_id = events[0].get("run_id") if events else None
        for artifact in self.artifacts.artifacts_for_run(run_id) if run_id else []:
            if artifact.get("kind") != "submission":
                continue
            binding = (artifact.get("metadata") or {}).get("controller_binding")
            if not isinstance(binding, dict) or binding.get("module") != module:
                continue
            if binding.get("pipeline_id") != expected_pipeline:
                continue
            return {
                "artifact_id": artifact.get("artifact_id"),
                "sha256": artifact.get("sha256"),
                "source_revisions": binding.get("source_revisions"),
            }
        return None

    def _validated_cached_ipipe_ingestion(
        self, run_id: str, existing: Any
    ) -> dict[str, Any]:
        if not isinstance(existing, dict):
            return _failure("STALE_ACTION", run_id=run_id)
        artifact_id = existing.get("artifact_id")
        artifact = self.artifacts.phase_artifact(artifact_id) if isinstance(artifact_id, str) else {}
        if not artifact.get("valid"):
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        envelope = artifact["envelope"]
        if envelope.get("run_id") != run_id or envelope.get("phase") != "IPIPE" or envelope.get("task_id") is not None:
            return _failure("ARTIFACT_INTEGRITY_FAILED", run_id=run_id)
        receipt_error = self._receipt_error(run_id, envelope, existing.get("knowledge_receipt"))
        if receipt_error is not None:
            return _failure(receipt_error, run_id=run_id)
        events = self.state.events(run_id)
        source_event = next((
            event for event in events if event.get("event_id") == envelope.get("source_event_id")
        ), None)
        payload = source_event.get("payload") if isinstance(source_event, dict) else None
        binding_error = self._ipipe_binding_error(events, payload, envelope.get("content"))
        if binding_error is not None:
            return _failure(binding_error, run_id=run_id)
        approval_error = self._controller_approval_error(run_id, {
            "approval_id": envelope.get("approval_id"),
            "approval_input_hash": envelope.get("approval_input_hash"),
        }, "G7")
        if approval_error is not None:
            return _failure(approval_error, run_id=run_id)
        return existing


def _failure(reason_code: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "phase_complete": False, **details}


def _exception_reason(error: Exception, fallback: str) -> str:
    reason = str(error)
    return reason if isinstance(error, ValueError) and reason in _STABLE_DEPENDENCY_REASONS else fallback


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _envelope_hash(envelope: dict[str, Any]) -> str:
    return _canonical_hash(envelope.get("content"))


def _approval_input_hash(
    action: dict[str, Any], content_hash: str, source_revisions: Any = None
) -> str:
    if action.get("phase") == "INTAKE":
        return action["input_hash"]
    return _canonical_hash(
        {
            "action_id": action["action_id"],
            "task_id": action.get("task_id"),
            "parent_artifact_hash": action.get("parent_artifact_hash"),
            "source_revisions": (
                source_revisions
                if isinstance(source_revisions, dict)
                else action.get("source_revisions", {})
            ),
            "content_hash": content_hash,
        }
    )


def _merge_evidence_refs(*groups: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for reference in validate_evidence_refs(group):
            if reference not in seen:
                seen.add(reference)
                merged.append(reference)
    return validate_evidence_refs(merged)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _valid_revisions(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and {"business", "tests"}.issubset(value)
        and all(isinstance(revision, str) and revision for revision in value.values())
    )


def _canonical_ku_url(value: Any, doc_id: Any) -> bool:
    if not isinstance(value, str) or not isinstance(doc_id, str) or not doc_id:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "ku.baidu-int.com"
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/").endswith(f"/{doc_id}")
    )


def _action_key(run_id: Any, source_event_id: Any, state: Any, task_id: Any, profile_hash: Any) -> str:
    """Identity of a phase action, including the profile it was computed against.

    The action embeds the pinned `profile_hash` in its own input hash, so an approved
    re-pin legitimately produces a different action for the same event. Without the
    hash in the cache key the recomputed action collides with the cached one and the
    run reports ACTION_CONFLICT forever, even though nothing is actually in conflict.
    """
    return f"phase-action:{run_id}:{source_event_id}:{state}:{task_id or '-'}:{profile_hash or '-'}"


def _registered_pipeline(pipeline: Any, module: Any) -> Any:
    """The pipeline registered for a module, else the profile's single pipeline."""
    entries = pipeline.get("pipelines") if isinstance(pipeline, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("module") == module:
            return entry.get("pipeline_id")
    return pipeline.get("pipeline_id") if isinstance(pipeline, dict) else None


def _registered_release_rule(pipeline: Any, module: Any) -> Any:
    entries = pipeline.get("pipelines") if isinstance(pipeline, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("module") == module:
            return entry.get("release_rule", pipeline.get("release_rule"))
    return pipeline.get("release_rule") if isinstance(pipeline, dict) else None


def _required_modules(pipeline: Any) -> list[str]:
    """Modules whose pipeline has to pass before the run may leave IPIPE."""
    entries = pipeline.get("pipelines") if isinstance(pipeline, dict) else None
    return sorted({
        entry["module"] for entry in entries or []
        if isinstance(entry, dict) and entry.get("required_for_release")
        and isinstance(entry.get("module"), str)
    })


def _pinned_profile_hash(events: list[dict[str, Any]], repin: Any = None) -> str | None:
    if isinstance(repin, dict) and isinstance(repin.get("new_hash"), str) and repin["new_hash"]:
        return repin["new_hash"]
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    value = payload.get("profile_hash")
    return value if isinstance(value, str) and value else None


def _pinned_profile_error(events: list[dict[str, Any]], repin: Any = None) -> str | None:
    """Compare the profile on disk against the hash this run is pinned to.

    An approved re-pin moves that hash, so it has to be consulted here too: this is a
    second, independent copy of the check `orchestrator._runtime_profile` performs,
    and a re-pin only one of them honours would leave the run healthy through one
    door and conflicted through the other.
    """
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    expected = _pinned_profile_hash(events, repin)
    if not _valid_hash(expected):
        return "PROJECT_NOT_READY"
    profile_path = payload.get("profile_path")
    if profile_path is None:
        return None
    if not isinstance(profile_path, str) or not profile_path:
        return "PROJECT_NOT_READY"
    try:
        actual = hashlib.sha256(Path(profile_path).read_bytes()).hexdigest()
    except OSError:
        return "PROJECT_NOT_READY"
    return None if actual == expected else "PROFILE_CONFLICT"


def _requirement_id(events: list[dict[str, Any]]) -> str:
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    value = payload.get("requirement_id")
    return value if isinstance(value, str) and value else "UNKNOWN"


def _intake_snapshot(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    snapshot = payload.get("requirement_snapshot")
    return json.loads(_canonical_json(snapshot)) if isinstance(snapshot, dict) else None


def _intake_prerequisites(events: list[dict[str, Any]]) -> dict[str, Any]:
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    return intake_prerequisites(payload)


def _auto_grill_decision_log(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic GRILL decision-log for the express class.

    Only used when the card already carries acceptance criteria (guarded in `_next`),
    so there are no open decisions and nothing is added to the acceptance union — the
    Spec traceability still covers the snapshot's own acceptance points.
    """
    card_id = snapshot.get("canonical_card_id") if isinstance(snapshot, dict) else None
    return {
        "decision_result": "NO_OPEN_DECISIONS",
        "status": "COMPLETE",
        "decisions": [],
        "unresolved_frontier": [],
        "glossary_delta": {},
        "adr_candidates": [],
        "source_evidence": [f"icafe:{card_id}/snapshot"],
        "acceptance_delta": [],
    }


def _ipipe_controller_binding(payload: Any) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    return {
        key: source.get(key)
        for key in (
            "pipeline_id", "module", "release_rule", "source_revisions",
            "environment_fingerprint", "submission_artifact_id", "submission_hash",
            "approval_id", "approval_input_hash",
        )
    }


def _source_evidence_refs(
    events: list[dict[str, Any]], predecessor: dict[str, Any] | None
) -> list[str]:
    if predecessor is not None:
        references = predecessor.get("envelope", {}).get("evidence_refs")
        return list(references) if isinstance(references, list) else []
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    references = payload.get("evidence_refs")
    return list(references) if isinstance(references, list) else []


def _artifact_reference(artifact: dict[str, Any]) -> dict[str, Any]:
    envelope = artifact["envelope"]
    return {
        "artifact_id": artifact["artifact_id"], "phase": envelope["phase"],
        "task_id": envelope["task_id"], "content_hash": envelope["content_hash"],
    }


def _allowed_side_effects(state: str, controller: bool) -> list[str]:
    if controller:
        return {
            "WORKSPACE": ["workspace.inspect", "workspace.create", "state.transition"],
            "SUBMIT": ["icode.submit", "state.transition"],
            "IPIPE": ["ipipe.trigger", "ipipe.monitor", "artifact.write", "knowledge.publish", "icafe.comment"],
            "RELEASE": ["release.verify", "artifact.write", "knowledge.publish", "icafe.comment", "state.transition"],
        }[state]
    return ["artifact.write", "knowledge.publish", "icafe.comment", "state.transition"]


def _completion_predicate(state: str, schema: str | None) -> dict[str, Any]:
    return {
        "state": state,
        "schema": schema,
        "accepts_phase_result": schema is not None,
        "requires_local_integrity": schema is not None,
        "requires_ku_receipt": schema is not None,
        "requires_icafe_receipt": schema is not None,
        "requires_legal_transition": True,
    }


def _passing_review(content: Any) -> bool:
    if not isinstance(content, dict):
        return False
    if content.get("verdict") != "ACCEPT" or content.get("completeness_state") != "COMPLETE":
        return False
    axes = content.get("axes")
    if not isinstance(axes, dict) or not axes:
        return False
    if any(not isinstance(axis, dict) or axis.get("complete") is not True for axis in axes.values()):
        return False
    findings = content.get("findings")
    if not isinstance(findings, list):
        return False
    # Checked here as well as in `_validate_review`, because this is the function the
    # SUBMIT gate and the DAG frontier actually ask, and an unanswered question is not a
    # pass however the verdict field reads. A `NEEDS_CLARIFICATION` finding says nobody
    # has established whether the code is wrong; treating that as clean is exactly the
    # "do not implement an unverified suggestion" rule read backwards.
    return not any(
        isinstance(finding, dict)
        and (finding.get("blocking") is True or finding.get("classification") == "NEEDS_CLARIFICATION")
        for finding in findings
    )


def _phase_title(action: dict[str, Any], attempt: int = 1) -> str:
    base = _TITLE[action["phase"]]
    task_id = action.get("task_id")
    if action["phase"] in _TASK_SCOPED_TITLES:
        base = f"{base}/{task_id}"
    elif action["phase"] == "DIAGNOSE":
        base = f"{base}/1"
    # A phase artifact in KU is immutable: the document carries the hash of what was
    # written to it, so a re-entered phase (a re-cut DAG) cannot overwrite the title it
    # used before. The attempt keeps each artifact its own document, which is also the
    # honest record — the discarded artifact stays next to the approval it lost.
    return base if attempt <= 1 else f"{base}-r{attempt}"


def _phase_attempt(events: list[dict[str, Any]], phase: str, task_id: Any) -> int:
    """How many times this run has entered this phase, in the title's own scope.

    The counter exists only to keep an immutable KU title unique, so it has to be
    counted in exactly the scope the title is qualified by. A task-scoped title is
    entered once per task, so it is counted per task or the second task's first plan
    would look like the first task's second attempt. A run-scoped title is counted per
    run: a repair re-enters SPEC and TASKS carrying the diagnosis' task_id, and counting
    that entry in its own bucket would hand it the title the original run-scoped entry
    already published, which KU refuses as immutable.
    """
    task_scoped = phase in _TASK_SCOPED_TITLES
    return sum(
        1
        for event in events
        if event.get("state") == phase
        and (not task_scoped or _event_task_id(event) == task_id)
    )


def _event_task_id(event: dict[str, Any]) -> Any:
    payload = event.get("payload")
    return payload.get("task_id") if isinstance(payload, dict) else None
