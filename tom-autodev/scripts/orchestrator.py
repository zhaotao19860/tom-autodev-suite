from __future__ import annotations

import hashlib
import json
import uuid
import re
from pathlib import Path
from typing import Any

from approval_contract import (
    gateway_result_matches_request,
    is_clean_initial_pending_result,
    parse_gateway_result,
)
from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
from collaboration import (
    CollaborationSession,
    build_collaboration_binding,
    intake_input_hash,
)
from evidence_gate import EvidenceGate
from evidence_policy import requirement_for
from knowledge_sync import KnowledgeSync
from phase_protocol import PhaseProtocol
from project_registry import load_profile, profile_path
from requirement_snapshot import validation_error as snapshot_validation_error
from recovery import Recovery
from state_store import StateStore
from transition_policy import ALLOWED_TRANSITIONS, TransitionPolicy
from workspace_manager import WorkspaceManager


_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class Orchestrator:
    def __init__(self, config_root: Path | str | None = None):
        self.config_root = Path(config_root or (Path.home() / ".tom-autodev")).expanduser()
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
    ) -> dict[str, Any]:
        configured_profile_path = profile_path(project, self.config_root)
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
        snapshot_identity = requirement_snapshot["content_hash"]
        idempotency_key = f"start:{project}:{requirement_id}:{profile_hash}:{snapshot_identity}"
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

    def status(self, run_id: str) -> dict[str, Any]:
        events = self.state.events(run_id)
        if not events:
            return {"run_id": run_id, "state": "RUN_NOT_FOUND", "events": []}
        latest = events[-1]
        return {"run_id": run_id, "state": latest["state"], "events": events}

    def next(self, run_id: str) -> dict[str, Any]:
        """Return the next Comate-owned phase/controller action without remote writes."""
        return self.phase_protocol().next(run_id)

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
        return self.phase_protocol(sync).complete(run_id, envelope)

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
        recorded_hash = intake.get("profile_hash")
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
        if recorded_path != str(profile_path(recorded_project, self.config_root)):
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
        ku = next(
            (
                item for item in pinned["profile"].get("knowledge_sources", [])
                if isinstance(item, dict) and item.get("provider") == "ku"
            ),
            {},
        )
        if (
            not isinstance(sync, KnowledgeSync)
            or sync.state is not self.state
            or sync.run_id != run_id
            or sync.card_id != intake.get("requirement_id")
            or sync.project_parent_doc_id != ku.get("parent_doc_id")
            or sync.parent_doc_id is not None
        ):
            return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_MISMATCH", "run_id": run_id}
        return sync

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

        gate_context = self._ledger_backed_evidence(run_id, current_state, next_state, effective_evidence)
        gate = self.evidence_gate.check(next_state, gate_context, current_state)
        if not gate["passed"]:
            return {"run_id": run_id, "state": current_state, **gate}

        event = self.state.transition(
            run_id,
            next_state,
            {
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
            },
        )
        result = {
            "run_id": run_id,
            "state": next_state,
            "event_id": event["event_id"],
            "reason_code": "OK",
        }
        self.state.save_idempotency_result(idempotency_key, result)
        return result

    def route_failure(
        self,
        run_id: str,
        reason_code: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.status(run_id)
        if current["state"] == "RUN_NOT_FOUND":
            return current
        recovery_block = self._recovery_block(run_id, current["state"])
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
            else self.transition_policy.failure_target(current["state"], reason_code)
        )
        transition = self.transition_policy.validate(current["state"], next_state)
        if not transition["allowed"]:
            return {
                "run_id": run_id,
                "state": current["state"],
                **transition,
            }

        event = self.state.transition(
            run_id,
            next_state,
            {"reason_code": reason_code, "evidence": routed_evidence, "policy_decision": transition},
        )
        result = {
            "run_id": run_id,
            "state": next_state,
            "event_id": event["event_id"],
            "reason_code": reason_code,
            "collaboration_category": collaboration_category,
        }
        self.state.save_idempotency_result(idempotency_key, result)
        return result

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
        return context

    def _recovery_block(self, run_id: str, current_state: str) -> dict[str, Any] | None:
        recovery = self.recovery.resume(run_id)
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
        return self.approvals.resolve(approval_id, decision, input_hash, channel, run_id=run_id, responder=responder, state_store=self.state)

    def collaboration_session(self, group_client: Any) -> CollaborationSession:
        """Create the per-run collaboration adapter with an explicitly supplied boundary."""
        return CollaborationSession(self.state, group_client, approvals=self.approvals)

    def icode_runtime(self, run_id: str, **options: Any) -> Any:
        if self.status(run_id)["state"] == "RUN_NOT_FOUND":
            return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        if set(options) & {"state_store", "approval_ledger", "artifact_store", "workspace_manager", "run_id"}:
            return {"ok": False, "reason_code": "RUNTIME_OPTION_FORBIDDEN", "run_id": run_id}
        from clients.icode_runtime import IcodeRuntime

        return IcodeRuntime(
            state_store=self.state,
            approval_ledger=self.approvals,
            artifact_store=self.artifacts,
            workspace_manager=self.workspaces,
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
    ) -> dict[str, Any]:
        """Submit through the owned iCode boundary and form the IPIPE checkpoint."""
        try:
            return self._submit_to_ipipe(run_id, change_set, approval, icode_runtime)
        except Exception:
            return {"ok": False, "reason_code": "SUBMISSION_BINDING_FAILED", "run_id": run_id}

    def _submit_to_ipipe(
        self,
        run_id: str,
        change_set: Any,
        approval: Any,
        icode_runtime: Any,
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

        submitted = icode_runtime.submit(change_set, approval)
        if not isinstance(submitted, dict) or submitted.get("reason_code") != "OK" or not submitted.get("ok"):
            return submitted if isinstance(submitted, dict) else {
                "ok": False, "reason_code": "ICODE_RECEIPT_INVALID", "run_id": run_id,
            }
        submit_key = f"icode.submit:{run_id}:{change_set_id}:{revision_set_id}"
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
        if intent != expected_intent:
            return {"ok": False, "reason_code": "ICODE_RECEIPT_MISMATCH", "run_id": run_id}
        approval_record = self.approvals.get(approval_id) if isinstance(approval_id, str) else None
        if not isinstance(approval_record, dict):
            return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id}
        if approval_record.get("run_id") != run_id or approval_record.get("run_id") == "legacy":
            return {"ok": False, "reason_code": "APPROVAL_RUN_MISMATCH", "run_id": run_id}
        if approval_record.get("action") != "G7":
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
        content = json.dumps(
            submitted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        submission = self.artifacts.put(run_id, "submission", content, {
            "controller_binding": controller_binding,
            "icode_intent_id": durable["intent"]["intent_id"],
            "change_set_id": change_set_id,
            "revision_set_id": revision_set_id,
        })
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
        payload = {
            "previous_state": "SUBMIT",
            "requirement_id": intake["requirement_id"],
            "project": intake["project"],
            "profile_path": intake["profile_path"],
            "profile_hash": intake["profile_hash"],
            **controller_binding,
            "submission_artifact_id": submission["artifact_id"],
            "submission_hash": submission["sha256"],
            "approval_id": approval_id,
            "approval_input_hash": input_hash,
            "policy_decision": transition,
        }
        committed = self.state.commit_transition_result(
            run_id,
            events[-1]["event_id"],
            "IPIPE",
            payload,
            result_key,
            {
                "ok": True,
                "reason_code": "OK",
                "submission_artifact_id": submission["artifact_id"],
                "submission_hash": submission["sha256"],
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
        recorded_hash = intake.get("profile_hash")
        recorded_project = intake.get("project")
        if not all(isinstance(value, str) and value for value in (recorded_path, recorded_hash, recorded_project)):
            return {"ok": False, "reason_code": "PROJECT_NOT_READY", "run_id": run_id}
        if recorded_path != str(profile_path(recorded_project, self.config_root)):
            return {"ok": False, "reason_code": "PROJECT_PROFILE_PATH_MISMATCH", "run_id": run_id}
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
        for channel, client in (("comate", comate_client), ("infoflow", infoflow_client)):
            envelope = {"channel": channel, "approval": payload}
            outcome = self._deliver_approval_channel(run_id, request["approval_id"], channel, client, envelope, input_hash)
            if outcome.get("reason_code") not in {None, "OK"}:
                return {**(self.approvals.get(request["approval_id"]) or request), **outcome}
        return self.approvals.get(request["approval_id"]) or request

    def _deliver_approval_channel(
        self, run_id: str, approval_id: str, channel: str, client: Any, payload: dict[str, Any], payload_hash: str
    ) -> dict[str, Any]:
        key = f"approval.delivery:{approval_id}:{channel}"
        canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical_payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        claim = self.state.claim_intent(run_id, f"approval.delivery.{channel}", key, {"approval_id": approval_id, "canonical_payload_sha256": canonical_payload_hash})
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
        if claim["status"] == "EXISTING":
            reconciled = client.reconcile(payload) if hasattr(client, "reconcile") else None
            if reconciled is None:
                return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
            response = reconciled
        else:
            try:
                response = client.request(payload)
            except ValueError as error:
                if channel == "infoflow" and str(error) == "APPROVAL_REQUEST_RESPONSE_INVALID":
                    failure = {"reason_code": "APPROVAL_REQUEST_RESPONSE_INVALID"}
                    return self.state.save_idempotency_result(failure_key, failure)
                reconciled = client.reconcile(payload) if hasattr(client, "reconcile") else None
                if reconciled is None:
                    return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
                response = reconciled
            except Exception:
                reconciled = client.reconcile(payload) if hasattr(client, "reconcile") else None
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

    def wait_infoflow_approval(self, run_id: str, approval_id: str, input_hash: str, infoflow_client: Any, timeout_seconds: float) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.get("run_id") != run_id or approval.get("run_id") == "legacy":
            return {"reason_code": "APPROVAL_RUN_MISMATCH", "run_id": run_id}
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

    def heartbeat_infoflow_approval(self, run_id: str, approval_id: str, observed_at: str) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.get("run_id") != run_id or approval.get("run_id") == "legacy":
            return {"run_id": run_id, "reason_code": "APPROVAL_RUN_MISMATCH"}
        return self.approvals.record_heartbeat(approval_id, observed_at)

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


def _with_ku_acceptance(
    snapshot: Any, project: str, config_root: Any = None
) -> Any:
    """Fill an empty iCafe acceptance list from the configured KU requirement doc.

    Some spaces keep acceptance criteria in the KU analysis document instead of an iCafe
    property. Provenance is recorded in `acceptance_source` so a later gate can tell
    KU-sourced acceptance apart from acceptance the card itself declared.
    """
    if not isinstance(snapshot, dict) or snapshot.get("acceptance"):
        return snapshot
    from clients.ku_client import KuClient
    from project_registry import load_profile, profile_path

    loaded = load_profile(profile_path(project, config_root))
    if not loaded.get("ready"):
        return snapshot
    ku_sources = sorted(
        (
            item
            for item in loaded["profile"].get("knowledge_sources", [])
            if isinstance(item, dict) and item.get("provider") == "ku"
        ),
        key=lambda item: item.get("priority", 0),
    )
    if not ku_sources:
        return snapshot
    source = ku_sources[0]
    found = KuClient(repo_id=source["repo_id"]).requirement_acceptance(
        source["parent_doc_id"]
    )
    if not found.get("ok"):
        return snapshot
    from requirement_snapshot import content_hash as requirement_snapshot_hash

    augmented = {
        **snapshot,
        "acceptance": found["acceptance"],
        "acceptance_source": found["source"],
    }
    # The hash covers the acceptance and its provenance, so the run is invalidated when
    # either the card or the KU analysis document changes.
    augmented["content_hash"] = requirement_snapshot_hash(augmented)
    return augmented


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
    owned_business = next((
        repository for repository in repositories
        if isinstance(repository, dict) and repository.get("module") == receipt.get("module")
    ), None)
    if (
        not isinstance(owned_business, dict)
        or business.get("module") != owned_business.get("module")
        or business.get("branch") != owned_business.get("branch")
        or tests.get("module") != test_repository.get("module")
        or tests.get("branch") != test_repository.get("branch")
        or receipt.get("module") != change_set.get("module")
        or receipt.get("commit_revision") != business.get("revision")
        or receipt.get("patchset") != business.get("revision")
        or receipt.get("revision_set") != change_set.get("revision_set")
    ):
        return "SOURCE_REVISION_MISMATCH", {}
    source_revisions = {
        "business": business.get("revision"),
        "tests": tests.get("revision"),
    }
    if not all(isinstance(value, str) and value for value in source_revisions.values()):
        return "SOURCE_REVISION_REQUIRED", {}
    pipeline_id = pipeline.get("pipeline_id")
    release_rule = pipeline.get("release_rule")
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

    status = subparsers.add_parser("status")
    status.add_argument("run_id")

    approve = subparsers.add_parser("approve")
    approve.add_argument("approval_id")
    approve.add_argument("decision")
    approve.add_argument("input_hash")
    approve.add_argument("channel")
    approve.add_argument("run_id")
    approve.add_argument("responder")

    resume = subparsers.add_parser("resume")
    resume.add_argument("run_id")

    stop = subparsers.add_parser("stop")
    stop.add_argument("run_id")

    next_action = subparsers.add_parser("next")
    next_action.add_argument("run_id")

    complete = subparsers.add_parser("complete-phase")
    complete.add_argument("run_id")
    complete.add_argument("envelope")

    optimize = subparsers.add_parser("optimize")
    optimize.add_argument("run_id")
    optimize.add_argument("operation", choices=("build", "propose", "apply"))
    optimize.add_argument("--summary")
    optimize.add_argument("--allowed-root", action="append", default=[])
    optimize.add_argument("--proposal-id")
    optimize.add_argument("--approval-id")

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("project")

    args = parser.parse_args(argv)
    orchestrator = Orchestrator(args.config_root)
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
            snapshot = _with_ku_acceptance(snapshot, args.project, args.config_root)
            result = orchestrator.start(
                args.requirement_id, args.project, requirement_snapshot=snapshot
            )
    elif args.command == "status":
        result = orchestrator.status(args.run_id)
    elif args.command == "approve":
        result = orchestrator.approve(args.approval_id, args.decision, args.input_hash, args.channel, args.run_id, args.responder)
    elif args.command == "resume":
        result = orchestrator.resume(args.run_id)
    elif args.command == "stop":
        result = orchestrator.stop(args.run_id)
    elif args.command == "next":
        result = orchestrator.next(args.run_id)
    elif args.command == "complete-phase":
        envelope = _json_file(args.envelope)
        result = (
            orchestrator.complete_phase(args.run_id, envelope)
            if isinstance(envelope, dict)
            else envelope
        )
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
            result = orchestrator.optimize(
                args.run_id,
                args.operation,
                proposal_id=args.proposal_id,
                approval_id=args.approval_id,
            )
        else:
            result = orchestrator.optimize(args.run_id, args.operation, **options)
    else:
        result = orchestrator.preflight(args.project)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return _cli_exit_code(result)


def _cli_exit_code(result: Any) -> int:
    if not isinstance(result, dict):
        return 1
    if result.get("ready") is False or result.get("ok") is False:
        return 1
    if result.get("state") == "RUN_NOT_FOUND":
        return 1
    reason = result.get("reason_code")
    return 0 if reason in {None, "OK", "READY"} else 1


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


if __name__ == "__main__":
    raise SystemExit(main())
