from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from approval_ledger import ApprovalLedger
from clients.ipipe_client import IpipeTransportError
from project_registry import validate_profile
from state_store import StateStore


_FAILURE = frozenset({"FAIL", "FAILED", "ERROR", "ABORTED", "CANCELLED"})
_SUCCESS = frozenset({"SUCCESS", "SUCC", "SUCCEEDED", "PASSED", "PASS"})
_MANUAL = frozenset({"WAITING_FOR_MANUAL", "MANUAL", "PAUSED", "WAITING_INPUT", "WAITING_FOR_CONFIRM"})


class IpipeRuntime:
    """Run-bound iPipe discovery, write, monitoring, and release verification."""

    def __init__(
        self,
        state_store: StateStore,
        approval_ledger: ApprovalLedger,
        run_id: str,
        api_transport: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        poll_interval: float = 5,
        max_polls: int = 720,
        log_limit: int = 4096,
        validated_profile: dict[str, Any] | None = None,
        profile_hash: str | None = None,
    ):
        self.state = state_store
        self.approvals = approval_ledger
        self.run_id = run_id
        self.api = api_transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper or time.sleep
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.log_limit = log_limit
        self.validated_profile = validated_profile
        self.profile_hash = profile_hash
        self._build_bindings: dict[str, dict[str, Any]] = {}
        self._stage_bindings: dict[str, dict[str, Any]] = {}

    def discover(self, profile: dict[str, Any], revision_set: dict[str, Any]) -> dict[str, Any]:
        context = self._context(profile, revision_set)
        if context.get("reason_code") != "OK":
            return context
        try:
            pipeline = self.api.get_pipeline_by_id(context["pipeline_id"])
        except Exception as error:
            return _failure(_transport_reason(error, "PIPELINE_QUERY_FAILED"))
        if (
            str(pipeline.get("id") or pipeline.get("pipelineConfId") or "") != context["pipeline_id"]
            or str(pipeline.get("module") or pipeline.get("space") or "") != context["module"]
        ):
            return _failure("PIPELINE_IDENTITY_MISMATCH")
        primary_revision = context["revision_map"][context["module"]]
        try:
            candidates = self.api.builds_by_revision(context["module"], primary_revision, context["pipeline_id"])
        except Exception as error:
            return _failure(_transport_reason(error, "BUILD_QUERY_FAILED"))
        matches = [candidate for candidate in candidates if _matches_build(candidate, context)]
        unique = {str(item.get("id") or item.get("pipelineBuildId")): item for item in matches}
        unique.pop("", None)
        if not unique:
            return _failure("BUILD_QUERY_REQUIRED", retry_allowed=False)
        if len(unique) != 1:
            return _failure("BUILD_AMBIGUOUS", retry_allowed=False)
        build_id, candidate = next(iter(unique.items()))
        self._bind_build(build_id, context, candidate)
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "build_id": build_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "revision_set_id": context["revision_set_id"],
            "evidence_refs": _build_evidence(build_id, context),
        }

    def trigger(
        self, profile: dict[str, Any], revision_set: dict[str, Any], approval: dict[str, Any]
    ) -> dict[str, Any]:
        context = self._context(profile, revision_set)
        if context.get("reason_code") != "OK":
            return context
        forbidden = set(context["parameters"]) - set(context["allowed_parameters"])
        if forbidden or _contains_secret_material(context["parameters"]):
            return _failure("PIPELINE_PARAMETER_FORBIDDEN", forbidden_parameters=sorted(forbidden))
        binding = {
            "operation": "ipipe.trigger",
            "run_id": self.run_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "revision_set_id": context["revision_set_id"],
            "repositories": context["repositories"],
            "environment_fingerprint": context["environment_fingerprint"],
            "parameters": context["parameters"],
        }
        input_hash = _canonical_hash(binding)
        approval_failure = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G8", input_hash=input_hash
        )
        if approval_failure is not None:
            return approval_failure
        payload = {**binding, "input_hash": input_hash, "approval_id": approval["approval_id"]}
        key = f"ipipe.trigger:{self.run_id}:{context['pipeline_id']}:{context['revision_set_id']}"
        claim = self.state.claim_intent(self.run_id, "ipipe.trigger", key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("TRIGGER_CONFLICT")
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
        intent = claim["intent"]

        existing = self.discover(profile, revision_set)
        if existing.get("reason_code") == "OK":
            try:
                candidate = self.api.build_by_id(existing["build_id"])
            except Exception as error:
                return _failure(
                    _transport_reason(error, "TRIGGER_CONFIRMATION_REQUIRED"),
                    intent_id=intent["intent_id"],
                    retry_allowed=False,
                )
            return self._trigger_receipt(intent["intent_id"], context, candidate)
        if existing.get("reason_code") != "BUILD_QUERY_REQUIRED":
            return {**existing, "intent_id": intent["intent_id"]}
        if claim["status"] == "EXISTING":
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)

        try:
            result = self.api.trigger_by_revision(
                context["pipeline_id"], context["revision_map"][context["module"]], context["parameters"]
            )
        except Exception as error:
            if isinstance(error, IpipeTransportError) and not error.transient:
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            reconciled = self.discover(profile, revision_set)
            if reconciled.get("reason_code") == "OK":
                try:
                    candidate = self.api.build_by_id(reconciled["build_id"])
                except Exception:
                    candidate = None
                if candidate is not None:
                    return self._trigger_receipt(intent["intent_id"], context, candidate)
            return _failure("TRIGGER_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        build_id = str(result.get("id") or result.get("pipelineBuildId") or "") if isinstance(result, dict) else ""
        if not build_id:
            return _failure("TRIGGER_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            candidate = self.api.build_by_id(build_id)
        except Exception as error:
            return _failure(
                _transport_reason(error, "TRIGGER_CONFIRMATION_REQUIRED"),
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        if not _matches_build(candidate, context):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        return self._trigger_receipt(intent["intent_id"], context, candidate)

    def monitor(self, build_id: str, deadline: str) -> dict[str, Any]:
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        parsed_deadline = _deadline(deadline)
        if parsed_deadline is None:
            return _failure("DEADLINE_INVALID")
        for poll in range(self.max_polls):
            try:
                build = self.api.build_by_id(build_id)
                stages = self.api.pipeline_stage_info(build_id)
            except Exception as error:
                return _failure(_transport_reason(error, "PIPELINE_TRANSIENT"), status="TRANSIENT")
            if _build_id(build) != build_id:
                return _failure("BUILD_IDENTITY_MISMATCH", status="INVALID")
            if not _matches_build(build, binding):
                return _failure("REVISION_MISMATCH", status="INVALID")
            normalized_stages = [_normalize_stage(stage) for stage in stages]
            stage_identity_failure = _stage_identity_failure(normalized_stages)
            if stage_identity_failure is not None:
                return _failure(stage_identity_failure, status="INVALID")
            for stage in normalized_stages:
                self._stage_bindings[stage["stage_build_id"]] = {
                    "build_id": build_id,
                    "context": binding,
                    "stage": stage,
                }
            failed = [stage for stage in normalized_stages if stage["status"] in _FAILURE]
            manual = [stage for stage in normalized_stages if stage["status"] in _MANUAL]
            aggregate = _status(build)
            if failed:
                return self._failure_evidence(build_id, binding, build, normalized_stages, failed)
            if aggregate in _FAILURE:
                return self._failure_evidence(build_id, binding, build, normalized_stages, [])
            if manual:
                stage = manual[0]
                return {
                    "ok": False,
                    "reason_code": "MANUAL_STAGE_WAIT",
                    "status": "MANUAL_WAIT",
                    "build_id": build_id,
                    "stage_build_id": stage["stage_build_id"],
                    "stages": normalized_stages,
                    "environment_fingerprint": binding["environment_fingerprint"],
                    "evidence_refs": _build_evidence(build_id, binding) + [f"ipipe:stage/{stage['stage_build_id']}"],
                }
            if aggregate in _SUCCESS and all(stage["status"] in _SUCCESS for stage in normalized_stages):
                return {
                    "ok": True,
                    "reason_code": "OK",
                    "status": "SUCCESS",
                    "classification": "SUCCESS",
                    "build_id": build_id,
                    "stages": normalized_stages,
                    "environment_fingerprint": binding["environment_fingerprint"],
                    "evidence_refs": _build_evidence(build_id, binding),
                }
            if self.clock().astimezone(timezone.utc) >= parsed_deadline:
                return {
                    "ok": False,
                    "reason_code": "MONITOR_TIMEOUT",
                    "status": "TIMEOUT",
                    "classification": "TIMEOUT",
                    "build_id": build_id,
                    "stages": normalized_stages,
                    "environment_fingerprint": binding["environment_fingerprint"],
                    "evidence_refs": _build_evidence(build_id, binding),
                }
            if poll + 1 < self.max_polls:
                self.sleeper(self.poll_interval)
        return _failure("MONITOR_TIMEOUT", status="TIMEOUT", build_id=build_id)

    def rerun(self, stage_build_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        key = f"ipipe.rerun:{self.run_id}:{stage_build_id}"
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            response = completed["receipt"]["response"]
            if not _valid_replay_approval(
                self.approvals, approval, completed["intent"]["payload"], run_id=self.run_id
            ):
                return _failure("RERUN_CONFLICT")
            return dict(response)
        existing = self.state.intent_by_idempotency_key(key)
        if existing is not None:
            if not _valid_replay_approval(
                self.approvals, approval, existing["payload"], run_id=self.run_id
            ):
                return _failure("RERUN_CONFLICT")
            reconciled = self._reconcile_rerun(existing)
            if reconciled is not None:
                return reconciled
            return _failure("QUERY_REQUIRED", intent_id=existing["intent_id"], retry_allowed=False)

        stage_binding = self._stage_bindings.get(stage_build_id)
        if stage_binding is None:
            return _failure("STAGE_OWNERSHIP_UNVERIFIED")
        stage = stage_binding["stage"]
        if stage["status"] not in _FAILURE | _MANUAL:
            return _failure("STAGE_NOT_RERUNNABLE")
        context = stage_binding["context"]
        build_id = stage_binding["build_id"]
        failure_signature = stage.get("failure_signature") or _failure_signature(build_id, [stage], [])
        binding = {
            "operation": "ipipe.rerun",
            "run_id": self.run_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "environment_fingerprint": context["environment_fingerprint"],
            "build_id": build_id,
            "stage_build_id": stage_build_id,
            "revision_set_id": context["revision_set_id"],
            "repositories": context["repositories"],
            "failure_signature": failure_signature,
        }
        input_hash = _canonical_hash(binding)
        approval_failure = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G8", input_hash=input_hash
        )
        if approval_failure is not None:
            return approval_failure
        payload = {
            **binding,
            "input_hash": input_hash,
            "approval_id": approval["approval_id"],
        }
        claim = self.state.claim_intent(self.run_id, "ipipe.rerun", key, payload)
        if claim["status"] != "CLAIMED":
            return _failure("RERUN_CONFLICT" if claim["status"] == "CONFLICT" else "QUERY_REQUIRED", retry_allowed=False)
        intent = claim["intent"]
        try:
            response = self.api.manual_execute_stage(stage_build_id, {})
        except Exception as error:
            if isinstance(error, IpipeTransportError) and not error.transient:
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            reconciled = self._reconcile_rerun(intent)
            if reconciled is not None and reconciled.get("reason_code") in {"OK", "REVISION_MISMATCH"}:
                return reconciled
            return _failure("RERUN_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        response_stage = str(response.get("stageBuildId") or response.get("id") or "") if isinstance(response, dict) else ""
        if response_stage != stage_build_id:
            return _failure("RERUN_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            current = self.api.build_by_id(build_id)
        except Exception as error:
            return _failure(
                _transport_reason(error, "RERUN_CONFIRMATION_REQUIRED"),
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        if not _matches_build(current, context):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        current_stage = next(
            (item for item in _embedded_stages(current) if str(item.get("id") or item.get("stageBuildId") or "") == stage_build_id),
            None,
        )
        if current_stage is None or _status(current_stage) in _FAILURE | _MANUAL:
            return _failure("RERUN_CONFIRMATION_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        return self._save_rerun_receipt(intent["intent_id"], payload)

    def _save_rerun_receipt(self, intent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "build_id": payload["build_id"],
            "stage_build_id": payload["stage_build_id"],
            "revision_set_id": payload["revision_set_id"],
            "evidence_refs": [
                f"ipipe:build/{payload['build_id']}",
                f"ipipe:stage/{payload['stage_build_id']}",
            ],
        }
        self.state.receipt(intent_id, receipt, receipt["evidence_refs"])
        return receipt

    def _reconcile_rerun(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        payload = intent["payload"]
        try:
            build = self.api.build_by_id(payload["build_id"])
        except Exception as error:
            if isinstance(error, IpipeTransportError):
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            return None
        expected_revisions = {
            item["module"]: item["revision"]
            for item in payload.get("repositories", [])
            if isinstance(item, dict) and _nonempty(item.get("module")) and _nonempty(item.get("revision"))
        }
        actual_revisions = build.get("revisions") if isinstance(build, dict) else None
        if actual_revisions != expected_revisions:
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        if payload.get("pipeline_id") and str(build.get("pipelineConfId") or build.get("pipeline_id") or "") != payload["pipeline_id"]:
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        if payload.get("module") and str(build.get("module") or build.get("space") or "") != payload["module"]:
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        current_stage = next(
            (
                item for item in _embedded_stages(build)
                if str(item.get("id") or item.get("stageBuildId") or "") == payload["stage_build_id"]
            ),
            None,
        )
        if current_stage is None or _status(current_stage) in _FAILURE | _MANUAL:
            return None
        return self._save_rerun_receipt(intent["intent_id"], payload)

    def verify_release(self, build_id: str, revision_set: dict[str, Any]) -> dict[str, Any]:
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        if revision_set.get("run_id") != self.run_id or revision_set.get("revision_set_id") != binding["revision_set_id"]:
            return _failure("REVISION_MISMATCH")
        if revision_set.get("repositories") != binding["repositories"]:
            return _failure("REVISION_MISMATCH")
        try:
            current = self.api.build_by_id(build_id)
        except Exception as error:
            return _failure(_transport_reason(error, "BUILD_QUERY_FAILED"))
        if _build_id(current) != build_id:
            return _failure("BUILD_IDENTITY_MISMATCH")
        if not _matches_build(current, binding):
            return _failure("REVISION_MISMATCH")
        if _status(current) not in _SUCCESS:
            return _failure("PIPELINE_NOT_SUCCESSFUL")
        try:
            stages = [_normalize_stage(stage) for stage in self.api.pipeline_stage_info(build_id)]
        except Exception as error:
            return _failure(_transport_reason(error, "STAGE_QUERY_FAILED"))
        stage_identity_failure = _stage_identity_failure(stages)
        if stage_identity_failure is not None:
            return _failure(stage_identity_failure)
        if any(stage["status"] not in _SUCCESS for stage in stages):
            return _failure("PIPELINE_NOT_SUCCESSFUL")
        try:
            releases = self.api.release_info(binding["module"], binding["target_branch"])
        except Exception as error:
            return _failure(_transport_reason(error, "RELEASE_QUERY_FAILED"))
        exact = []
        associated = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            same_identity = (
                str(release.get("module") or "") == binding["module"]
                and str(release.get("branch") or "") == binding["target_branch"]
                and str(release.get("pipelineBuildId") or release.get("build_id") or "") == build_id
            )
            if same_identity:
                associated.append(release)
            if (
                same_identity
                and release.get("revisions") == binding["revision_map"]
                and str(release.get("releaseRule") or release.get("release_rule") or "") == binding["release_rule"]
                and _status(release) in _SUCCESS
            ):
                exact.append(release)
        if not exact:
            return _failure("REVISION_MISMATCH" if associated else "RELEASE_WAITING", status="RELEASE_WAITING")
        if len(exact) != 1:
            return _failure("RELEASE_AMBIGUOUS")
        release_id = str(exact[0].get("id") or exact[0].get("releaseId") or "")
        if not release_id:
            return _failure("RELEASE_IDENTITY_MISSING")
        refs = _build_evidence(build_id, binding) + [f"ipipe:release/{release_id}"]
        return {
            "ok": True,
            "reason_code": "OK",
            "status": "SUCCESS",
            "run_id": self.run_id,
            "build_id": build_id,
            "release_id": release_id,
            "revision_set_id": binding["revision_set_id"],
            "release_rule": binding["release_rule"],
            "environment_fingerprint": binding["environment_fingerprint"],
            "evidence_refs": refs,
        }

    def _trigger_receipt(self, intent_id: str, context: dict[str, Any], build: dict[str, Any]) -> dict[str, Any]:
        if not _matches_build(build, context):
            return _failure("REVISION_MISMATCH", intent_id=intent_id, retry_allowed=False)
        build_id = str(build.get("id") or build.get("pipelineBuildId") or "")
        if not build_id:
            return _failure("TRIGGER_RESPONSE_INVALID", intent_id=intent_id, retry_allowed=False)
        self._bind_build(build_id, context, build)
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "build_id": build_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "revision_set_id": context["revision_set_id"],
            "parameters": context["parameters"],
            "environment_fingerprint": context["environment_fingerprint"],
            "evidence_refs": _build_evidence(build_id, context),
        }
        self.state.receipt(intent_id, response, response["evidence_refs"])
        return response

    def _context(self, profile: dict[str, Any], revision_set: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self.validated_profile, dict) or not _nonempty(self.profile_hash):
            return _failure("PROFILE_BINDING_REQUIRED")
        if profile != self.validated_profile:
            return _failure("PROFILE_CONFLICT")
        validation = validate_profile(profile, check_paths=False)
        if not validation.get("ready"):
            return _failure("PROJECT_NOT_READY")
        context = _context(profile, revision_set, self.run_id)
        if context.get("reason_code") == "OK":
            context["profile_hash"] = self.profile_hash
        return context

    def _bind_build(self, build_id: str, context: dict[str, Any], build: dict[str, Any]) -> None:
        binding = {
            key: context[key]
            for key in (
                "pipeline_id", "module", "revision_set_id", "repositories", "revision_map",
                "environment_fingerprint", "parameters", "target_branch", "release_rule", "stage_classes",
            )
        }
        self.state.save_idempotency_result(
            f"ipipe.build-binding:{self.run_id}:{build_id}",
            {"run_id": self.run_id, "build_id": build_id, "binding": binding},
        )
        self._build_bindings[build_id] = binding

    def _load_build_binding(self, build_id: str) -> dict[str, Any] | None:
        binding = self._build_bindings.get(build_id)
        if binding is not None:
            return binding
        stored = self.state.idempotency_result(f"ipipe.build-binding:{self.run_id}:{build_id}")
        if (
            not isinstance(stored, dict)
            or stored.get("run_id") != self.run_id
            or stored.get("build_id") != build_id
            or not isinstance(stored.get("binding"), dict)
        ):
            return None
        self._build_bindings[build_id] = stored["binding"]
        return stored["binding"]

    def _failure_evidence(
        self,
        build_id: str,
        binding: dict[str, Any],
        build: dict[str, Any],
        stages: list[dict[str, Any]],
        failed_stages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            jobs = [_normalize_job(job) for job in self.api.failed_jobs(build_id) if isinstance(job, dict)]
        except Exception as error:
            return _failure(_transport_reason(error, "FAILED_JOBS_QUERY_FAILED"))
        details = []
        for stage in failed_stages:
            try:
                details.append(self.api.stage_detail(stage["stage_build_id"]))
            except Exception as error:
                return _failure(_transport_reason(error, "STAGE_DETAIL_QUERY_FAILED"))
        excerpt = _log_text([jobs, details], self.log_limit)
        signature = _failure_signature(build_id, failed_stages, jobs)
        for stage in failed_stages:
            stage["failure_signature"] = signature
            self._stage_bindings[stage["stage_build_id"]]["stage"] = stage
        classification = _classification(failed_stages, jobs, binding["stage_classes"])
        refs = _build_evidence(build_id, binding)
        refs.extend(f"ipipe:stage/{stage['stage_build_id']}" for stage in failed_stages)
        refs.extend(
            f"ipipe:job/{job['job_build_id']}"
            for job in jobs if isinstance(job, dict) and job.get("job_build_id")
        )
        return {
            "ok": False,
            "reason_code": "PIPELINE_FAILED",
            "status": "FAILURE",
            "classification": classification,
            "build_id": build_id,
            "pipeline_status": _status(build),
            "stages": stages,
            "jobs": jobs,
            "log_excerpt": excerpt,
            "failure_signature": signature,
            "environment_fingerprint": binding["environment_fingerprint"],
            "evidence_refs": list(dict.fromkeys(refs)),
        }


def _context(profile: Any, revisions: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(profile, dict) or not isinstance(revisions, dict):
        return _failure("PROJECT_NOT_READY")
    pipeline = profile.get("pipeline_profile")
    business = profile.get("business_repos")
    test_repo = profile.get("test_repo")
    environment = profile.get("environment_profile")
    repositories = revisions.get("repositories")
    if (
        not isinstance(pipeline, dict) or not isinstance(business, list) or not business
        or not isinstance(test_repo, dict) or not isinstance(environment, dict)
        or not isinstance(repositories, list) or revisions.get("run_id") != run_id
        or not _nonempty(revisions.get("revision_set_id"))
    ):
        return _failure("PROJECT_NOT_READY")
    if not all(isinstance(item, dict) for item in business + [test_repo]) or not all(isinstance(item, dict) for item in repositories):
        return _failure("PROJECT_NOT_READY")
    pipeline_id = str(pipeline.get("pipeline_id") or "")
    allowed = pipeline.get("allowed_parameters")
    parameters = revisions.get("parameters", {})
    stage_classes = pipeline.get("stage_classes")
    release_rule = pipeline.get("release_rule")
    if not pipeline_id or not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed) or not isinstance(parameters, dict) or not isinstance(stage_classes, list) or not _nonempty(release_rule):
        return _failure("PROJECT_NOT_READY")
    expected = [("business", item.get("module"), item.get("branch")) for item in business]
    expected.append(("test", test_repo.get("module"), test_repo.get("branch")))
    actual = [(item.get("kind"), item.get("module"), item.get("branch")) for item in repositories]
    if expected != actual or any(not _nonempty(item.get("revision")) for item in repositories):
        return _failure("REVISION_SET_MISMATCH")
    revision_map = {item["module"]: item["revision"] for item in repositories}
    module = str(business[0].get("module") or "")
    return {
        "ok": True,
        "reason_code": "OK",
        "pipeline_id": pipeline_id,
        "module": module,
        "target_branch": str(business[0].get("branch") or ""),
        "revision_set_id": revisions["revision_set_id"],
        "repositories": repositories,
        "revision_map": revision_map,
        "allowed_parameters": allowed,
        "parameters": parameters,
        "stage_classes": stage_classes,
        "release_rule": release_rule,
        "environment_fingerprint": _canonical_hash(environment),
    }


def _matches_build(build: Any, context: dict[str, Any]) -> bool:
    if not isinstance(build, dict):
        return False
    pipeline_id = str(build.get("pipelineConfId") or build.get("pipeline_id") or "")
    module = str(build.get("module") or build.get("space") or "")
    revisions = build.get("revisions")
    if not isinstance(revisions, dict):
        revisions = {module: build.get("revision") or (build.get("trigger") or {}).get("revision")}
    primary_revision = build.get("revision") or (build.get("trigger") or {}).get("revision")
    return (
        pipeline_id == context["pipeline_id"]
        and module == context["module"]
        and revisions == context["revision_map"]
        and (primary_revision is None or primary_revision == context["revision_map"].get(context["module"]))
        and build.get("params", build.get("parameters", {})) == context["parameters"]
    )


def _build_id(build: Any) -> str:
    if not isinstance(build, dict):
        return ""
    return str(build.get("id") or build.get("pipelineBuildId") or build.get("build_id") or "")


def _stage_identity_failure(stages: list[dict[str, Any]]) -> str | None:
    stage_ids = [stage.get("stage_build_id") for stage in stages]
    if not stage_ids or any(not _nonempty(stage_id) for stage_id in stage_ids):
        return "STAGE_IDENTITY_MISSING"
    if len(set(stage_ids)) != len(stage_ids):
        return "STAGE_IDENTITY_MISMATCH"
    return None


def _normalize_stage(value: dict[str, Any]) -> dict[str, Any]:
    stage_id = str(value.get("id") or value.get("stageBuildId") or "")
    return {
        "stage_build_id": stage_id,
        "name": str(value.get("stageName") or value.get("name") or stage_id),
        "status": _status(value),
        "class": str(value.get("class") or value.get("stageClass") or ""),
    }


def _normalize_job(value: dict[str, Any]) -> dict[str, Any]:
    job_id = str(value.get("id") or value.get("jobBuildId") or "")
    return {
        "job_build_id": job_id,
        "name": str(value.get("jobName") or value.get("name") or job_id),
        "status": _status(value),
        "message": _redact_text(str(value.get("message") or value.get("statusMessage") or ""))[:1024],
    }


def _embedded_stages(build: dict[str, Any]) -> list[dict[str, Any]]:
    value = build.get("stageBuilds", build.get("stages", []))
    return value if isinstance(value, list) else []


def _status(value: dict[str, Any]) -> str:
    return str(value.get("status") or value.get("releaseStatus") or "UNKNOWN").upper()


def _classification(stages: list[dict[str, Any]], jobs: list[dict[str, Any]], stage_classes: list[str]) -> str:
    text = " ".join(
        [str(stage.get("name") or "") + " " + str(stage.get("class") or "") for stage in stages]
        + [str(job.get("message") or "") + " " + str(job.get("name") or "") for job in jobs if isinstance(job, dict)]
    ).lower()
    if any(word in text for word in ("environment", "capacity", "runner", "network", "timeout", "infra")):
        return "ENVIRONMENT_FAILURE"
    if any(word in text for word in ("unit", "test", "regression", "integration", "assert")):
        return "TEST_FAILURE"
    if any(word in text for word in ("compile", "build", "link", "code", "interface")):
        return "CODE_FAILURE"
    return "MIXED_FAILURE" if len(stages) > 1 else "PIPELINE_FAILURE"


def _failure_signature(build_id: str, stages: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> str:
    value = {
        "build_id": build_id,
        "stages": [{"id": item.get("stage_build_id"), "status": item.get("status")} for item in stages],
        "jobs": [{"id": item.get("job_build_id"), "status": item.get("status")} for item in jobs if isinstance(item, dict)],
    }
    return _canonical_hash(value)


def _log_text(value: Any, limit: int) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if sum(len(part) for part in parts) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in {"message", "statusmessage", "log", "logs", "outparams"}:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            parts.append(item)

    visit(value)
    return _redact_text("\n".join(parts))[:limit]


def _redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(x-ac-authorization|authorization)\s*[:=]\s*[^\s]+",
        lambda match: f"{match.group(1)}: [REDACTED]",
        value,
    )
    redacted = re.sub(r"(?i)\bBearer-[A-Za-z0-9._~+/=-]+", "[REDACTED]", redacted)
    return re.sub(
        r"(?i)\b(token|secret|password)\s*[:=]\s*[^\s]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )


def _contains_secret_material(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"(?i)(authorization|credential|password|private[_ -]?key|secret|token)", str(key)):
                return True
            if _contains_secret_material(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_secret_material(item) for item in value)
    if isinstance(value, str):
        return bool(re.search(r"(?i)\bBearer-[A-Za-z0-9._~+/=-]+|x-ac-Authorization\s*[:=]", value))
    return False


def _build_evidence(build_id: str, context: dict[str, Any]) -> list[str]:
    return [f"ipipe:build/{build_id}"] + [f"revision-{revision}" for revision in context["revision_map"].values()]


def _approved_record(
    ledger: ApprovalLedger, supplied: Any, *, run_id: str, action: str, input_hash: str
) -> dict[str, Any] | None:
    if not isinstance(supplied, dict) or not _nonempty(supplied.get("approval_id")):
        return _failure("APPROVAL_REQUIRED")
    record = ledger.get(supplied["approval_id"])
    if record is None:
        return _failure("APPROVAL_REQUIRED")
    if record.get("run_id") != run_id or record.get("run_id") == "legacy":
        return _failure("APPROVAL_RUN_MISMATCH")
    if record.get("action") != action:
        return _failure("APPROVAL_GATE_MISMATCH")
    if supplied.get("input_hash") != input_hash or record.get("input_hash") != input_hash:
        return _failure("APPROVAL_INPUT_MISMATCH")
    if record.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED")
    return None


def _valid_replay_approval(
    ledger: ApprovalLedger, supplied: Any, payload: dict[str, Any], *, run_id: str
) -> bool:
    if not isinstance(supplied, dict):
        return False
    if (
        supplied.get("approval_id") != payload.get("approval_id")
        or supplied.get("input_hash") != payload.get("input_hash")
    ):
        return False
    return _approved_record(
        ledger,
        supplied,
        run_id=run_id,
        action="G8",
        input_hash=str(payload.get("input_hash") or ""),
    ) is None


def _deadline(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _failure(reason_code: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **fields}


def _transport_reason(error: Exception, fallback: str) -> str:
    return error.reason_code if isinstance(error, IpipeTransportError) else fallback
