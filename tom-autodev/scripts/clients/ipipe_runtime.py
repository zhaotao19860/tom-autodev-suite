from __future__ import annotations

from execution_guard import guard_execution

import copy
import hashlib
import inspect
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from approval_ledger import ApprovalLedger, gate_of
import build_markers
from clients.ipipe_client import IpipeTransportError
from phase_protocol import _registered_pipeline
from profile_repin import pinned_hash, record_for
from project_registry import validate_profile
from state_store import StateStore


# iPipe reports the terminal verdict in both a gerund and a short form
# (`FAILING`/`FAIL`, `SUCCEEDING`/`SUCC`), and a stage that is waiting on a person is
# `PENDING_FOR_USER`. All three spellings appear on real builds.
_FAILURE = frozenset({"FAIL", "FAILING", "FAILED", "ERROR", "ABORTED", "CANCELLED", "CANCEL"})
_SUCCESS = frozenset({"SUCCESS", "SUCCEEDING", "SUCC", "SUCCEEDED", "PASSED", "PASS"})
_MANUAL = frozenset({
    "WAITING_FOR_MANUAL", "MANUAL", "PAUSED", "WAITING_INPUT", "WAITING_FOR_CONFIRM",
    "PENDING_FOR_USER",
})
_SKIPPED = frozenset({"SKIP", "SKIPPED"})

# `ipipe-cli trigger run --event-type`: a Gerrit patchset (CR) or a merged commit.
_REBUILD_EVENT_TYPES = frozenset({"GERRIT_CHANGE", "NEW_COMMIT"})
_REBUILD_REASON_LIMIT = 500
# A fresh re-build adopts an already-existing newer build instead of triggering another;
# a candidate older than the intent by more than this cannot have been started by it.
_REBUILD_CLOCK_SKEW_SECONDS = 120


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
        log_reader: Callable[[str], dict[str, Any]] | None = None,
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
        # Keep the factory's binding independent of a caller's mutable dictionary.
        self.validated_profile = copy.deepcopy(validated_profile)
        self.profile_hash = profile_hash
        self._bound_profile_hash = profile_hash
        self._profile_content_hash = _canonical_hash(self.validated_profile)
        # A job's status is its script's exit code. A stage that runs product cases without
        # checking their result reports success over a run where every case failed, and the
        # only place those numbers exist is the log, so the log is read before a build is
        # called passing.
        self.log_reader = log_reader or _default_log_reader
        self._build_bindings: dict[str, dict[str, Any]] = {}
        self._stage_bindings: dict[str, dict[str, Any]] = {}

    @guard_execution
    def discover(
        self, profile: dict[str, Any], revision_set: dict[str, Any], module: Any = None
    ) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        context = self._context(profile, revision_set, module)
        if context.get("reason_code") != "OK":
            return context
        try:
            pipeline = self.api.get_pipeline_by_id(context["pipeline_id"])
        except Exception as error:
            return _failure(_transport_reason(error, "PIPELINE_QUERY_FAILED"))
        if (
            str(pipeline.get("id") or pipeline.get("pipelineConfId") or "") != context["pipeline_id"]
            or context["module"] not in _pipeline_modules(pipeline)
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
        # A build this run replaced through `ipipe-rebuild` stays on record but is no
        # longer a candidate: two builds of one revision set would otherwise be ambiguous
        # for good, and the superseded one is exactly the one that must not be followed.
        for superseded_id in [key for key in unique if self.is_excluded(key)]:
            unique.pop(superseded_id)
        if not unique:
            return _failure("BUILD_QUERY_REQUIRED", retry_allowed=False)
        if len(unique) != 1:
            return _failure("BUILD_AMBIGUOUS", retry_allowed=False)
        build_id, candidate = next(iter(unique.items()))
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
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

    def _trigger_binding(self, context: dict[str, Any]) -> dict[str, Any]:
        """The canonical binding a trigger approval is bound to (shared by trigger and
        trigger_input_hash so the approved hash and the executed hash cannot drift)."""
        return trigger_binding(context, self.run_id)

    def trigger_input_hash(
        self, profile: dict[str, Any], revision_set: dict[str, Any], module: Any = None
    ) -> dict[str, Any]:
        """The trigger approval hash to request, without touching the pipeline.

        Lets the worker do compute-then-approve for the initial pipeline trigger the same
        way rerun_input_hash does for a rerun.
        """
        context = self._context(profile, revision_set, module)
        if context.get("reason_code") != "OK":
            return context
        return {
            "ok": True, "reason_code": "OK", "run_id": self.run_id,
            "input_hash": _canonical_hash(self._trigger_binding(context)),
        }

    @guard_execution
    def trigger(
        self,
        profile: dict[str, Any],
        revision_set: dict[str, Any],
        approval: dict[str, Any],
        module: Any = None,
    ) -> dict[str, Any]:
        context = self._context(profile, revision_set, module)
        if context.get("reason_code") != "OK":
            return context
        forbidden = set(context["parameters"]) - set(context["allowed_parameters"])
        if forbidden or _contains_secret_material(context["parameters"]):
            return _failure("PIPELINE_PARAMETER_FORBIDDEN", forbidden_parameters=sorted(forbidden))
        binding = self._trigger_binding(context)
        input_hash = _canonical_hash(binding)
        approval_failure = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G7", input_hash=input_hash
        )
        if approval_failure is not None:
            return approval_failure
        payload = {**binding, "input_hash": input_hash, "approval_id": approval["approval_id"]}
        key = f"ipipe.trigger:{self.run_id}:{context['pipeline_id']}:{context['revision_set_id']}"
        # A saved receipt is a read-only replay. Claiming/reconciling an intent is not.
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            if completed["intent"]["payload"] != payload:
                return _failure("TRIGGER_CONFLICT")
            replay = dict(completed["receipt"]["response"])
            if self.is_superseded(str(replay.get("build_id") or "")):
                return self._rebuilt_response(context, replay)
            return replay
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        claim = self.state.claim_intent(self.run_id, "ipipe.trigger", key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("TRIGGER_CONFLICT")
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            replay = dict(completed["receipt"]["response"])
            if self.is_superseded(str(replay.get("build_id") or "")):
                return self._rebuilt_response(context, replay)
            return replay
        intent = claim["intent"]

        existing = self.discover(profile, revision_set, context["module"])
        if existing.get("reason_code") == "OK":
            try:
                candidate = self.api.build_by_id(existing["build_id"], **self._build_key(context))
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

        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        try:
            result = self.api.trigger_by_revision(
                context["pipeline_id"], context["revision_map"][context["module"]], context["parameters"]
            )
        except Exception as error:
            if isinstance(error, IpipeTransportError) and not error.transient:
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            reconciled = self.discover(profile, revision_set, context["module"])
            if reconciled.get("reason_code") == "OK":
                try:
                    candidate = self.api.build_by_id(reconciled["build_id"], **self._build_key(context))
                except Exception:
                    candidate = None
                if candidate is not None:
                    return self._trigger_receipt(intent["intent_id"], context, candidate)
            return _failure("TRIGGER_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        build_id = str(result.get("id") or result.get("pipelineBuildId") or "") if isinstance(result, dict) else ""
        if not build_id:
            return _failure("TRIGGER_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            candidate = self.api.build_by_id(build_id, **self._build_key(context))
        except Exception as error:
            return _failure(
                _transport_reason(error, "TRIGGER_CONFIRMATION_REQUIRED"),
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        if not _matches_build(candidate, context):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        return self._trigger_receipt(intent["intent_id"], context, candidate)

    @guard_execution
    def monitor(
        self, build_id: str, deadline: str, *, poll_budget: int | None = None,
        park_on_budget: bool = False,
    ) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        parsed_deadline = _deadline(deadline)
        if parsed_deadline is None:
            return _failure("DEADLINE_INVALID")
        budget = self.max_polls if poll_budget is None else max(1, int(poll_budget))
        last_stages: list[dict[str, Any]] = []
        for poll in range(budget):
            blocked = self._profile_error()
            if blocked is not None:
                return blocked
            try:
                build = self.api.build_by_id(build_id, **self._build_key(binding))
                stages = self._build_stages(build_id, build)
            except Exception as error:
                return _failure(_transport_reason(error, "PIPELINE_TRANSIENT"), status="TRANSIENT")
            if _build_id(build) != build_id:
                return _failure("BUILD_IDENTITY_MISMATCH", status="INVALID")
            if not _matches_build(build, binding):
                return _failure("REVISION_MISMATCH", status="INVALID")
            normalized_stages = [_normalize_stage(stage) for stage in stages]
            last_stages = normalized_stages
            stage_identity_failure = _stage_identity_failure(normalized_stages)
            if stage_identity_failure is not None:
                return _failure(stage_identity_failure, status="INVALID")
            blocked = self._profile_error()
            if blocked is not None:
                return blocked
            for stage in normalized_stages:
                self._bind_stage(build_id, binding, stage)
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
                    "evidence_refs": _build_evidence(build_id, binding, normalized_stages),
                }
            if _build_passed(aggregate, self._mark_expression_skips(normalized_stages)):
                silent = self._silent_case_failures(normalized_stages)
                if silent:
                    return {
                        "ok": False,
                        "reason_code": "JOB_SUCCEEDED_WITH_FAILED_CASES",
                        "status": "FAILURE",
                        "classification": "TEST_FAILURE",
                        "build_id": build_id,
                        "stages": normalized_stages,
                        "case_failures": silent,
                        "environment_fingerprint": binding["environment_fingerprint"],
                        "evidence_refs": _build_evidence(build_id, binding, normalized_stages),
                    }
                return {
                    "ok": True,
                    "reason_code": "OK",
                    "status": "SUCCESS",
                    "classification": "SUCCESS",
                    "build_id": build_id,
                    "stages": normalized_stages,
                    "environment_fingerprint": binding["environment_fingerprint"],
                    "evidence_refs": _build_evidence(build_id, binding, normalized_stages),
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
                    "evidence_refs": _build_evidence(build_id, binding, normalized_stages),
                }
            if poll + 1 < budget:
                self.sleeper(self.poll_interval)
        if park_on_budget:
            return {
                "ok": True,
                "reason_code": "MONITORING",
                "status": "MONITORING",
                "build_id": build_id,
                "stages": last_stages,
                "environment_fingerprint": binding["environment_fingerprint"],
                "next_poll_after_seconds": self.poll_interval,
                "deadline": parsed_deadline.isoformat(),
                "evidence_refs": _build_evidence(build_id, binding, last_stages),
            }
        return _failure("MONITOR_TIMEOUT", status="TIMEOUT", build_id=build_id)

    def monitor_once(self, build_id: str, deadline: str) -> dict[str, Any]:
        """Observe one iPipe poll and return a durable-worker-friendly checkpoint.

        The legacy ``monitor`` method remains continuous for compatibility. Worker
        orchestration should use this bounded form so one drive call never occupies a
        lease for the lifetime of a long build.
        """
        return self.monitor(build_id, deadline, poll_budget=1, park_on_budget=True)

    def product_url(self, module: str, revision: str, pipeline_id: str) -> dict[str, Any]:
        """Reject the obsolete selector which cannot prove branch/change status."""
        # Compatibility entry point cannot prove the frozen change status or branch.
        return _failure("FROZEN_PRODUCT_PLAN_REQUIRED", module=module, revision=revision)

    def product_for_plan(self, plan: dict[str, Any], module: str) -> dict[str, Any]:
        from product_policy import target_for, select_product
        try:
            from pipeline_plan import frozen_plan
            if frozen_plan(self.state.events(self.run_id)) != plan:
                return _failure("PIPELINE_PLAN_MISMATCH")
            target = target_for(plan, self.run_id, module)
            pipelines = self.api.pipelines_by_module(module)
            ids = {str(p.get("id") or p.get("pipelineConfId") or "") for p in pipelines
                   if (p.get("name") or p.get("pipelineName")) == target["pipeline_name"]
                   and (p.get("module") or p.get("moduleName")) == module}
            if len(ids) != 1 or "" in ids:
                return _failure("PRODUCT_PIPELINE_IDENTITY_UNVERIFIED", module=module)
            pipeline_id = next(iter(ids))
            if target["changed"]:
                entry = (plan.get("product_inputs") or {}).get(module) or plan["modules"].get(module, {})
                if pipeline_id != str(entry.get("pipeline_id")):
                    return _failure("PRODUCT_PIPELINE_IDENTITY_UNVERIFIED", module=module)
                builds = self.api.builds_by_revision(module, target["revision"], pipeline_id)
            else:
                builds = self.api.branch_product_builds(module, pipeline_id)
            owned = set()
            fallback = set()
            from pipeline_plan import build_matches
            for build in builds:
                build_id = _build_id(build)
                if target["changed"]:
                    binding = self._load_build_binding(build_id)
                    branches = {r.get("module"): r.get("branch")
                                for r in (binding or {}).get("repositories", [])}
                    if (binding and not self.is_excluded(build_id)
                            and binding.get("pipeline_id") == pipeline_id
                            and binding.get("module") == module
                            and binding.get("revision_map", {}).get(module) == target["revision"]
                            and branches.get(module) == target["branch"]
                            and (module not in plan["modules"] or build_matches(
                                binding, plan["modules"][module]))):
                        owned.add(build_id)
                else:
                    capability = self.api.compile_only_capability()
                    if capability.get("supported"):
                        from product_policy import compile_binding
                        expected = compile_binding(target, pipeline_id, capability)
                        receipt = self.state.result_by_idempotency_key(
                            f"ipipe.branch-compile:{self.run_id}:{_canonical_hash(expected)}")
                        if receipt and receipt["receipt"]["response"].get("build_id") == build_id:
                            owned.add(build_id)
                            fallback.add(build_id)
            found = select_product(self.api, target, pipeline_id, builds,
                                   owned_build_ids=owned, compile_fallback_ids=fallback)
            if not found.get("ok") and not target["changed"] and found.get(
                "reason_code") in {"PRODUCT_URL_NOT_PUBLISHED", "PRODUCT_REVISION_MISMATCH"}:
                if len(builds) >= 100:
                    return _failure("BRANCH_PRODUCT_QUERY_INCOMPLETE", product_target=target)
                from product_policy import compile_binding
                try:
                    binding = compile_binding(target, pipeline_id, self.api.compile_only_capability())
                except ValueError as error:
                    # Keep the mismatch visible; never fall back to a newer release.
                    if found.get("reason_code") == "PRODUCT_REVISION_MISMATCH":
                        return found
                    return _failure(str(error), product_target=target)
                extra = ({"seen_revisions": found.get("seen_revisions")}
                         if found.get("reason_code") == "PRODUCT_REVISION_MISMATCH" else {})
                return _failure("BRANCH_COMPILE_APPROVAL_REQUIRED", gate="G8",
                                binding=binding, input_hash=_canonical_hash(binding), **extra)
            if found.get("ok"):
                if not hasattr(self, "_product_proofs"):
                    self._product_proofs = {}
                self._product_proofs[module] = found
            return found
        except ValueError as error:
            return _failure(str(error))
        except Exception as error:
            return _failure(_transport_reason(error, "PRODUCT_QUERY_FAILED"))

    @guard_execution
    def ensure_branch_compile(self, binding: dict[str, Any],
                              approval: dict[str, Any]) -> dict[str, Any]:
        from pipeline_plan import frozen_plan
        from product_policy import ensure_compile, target_for, compile_binding
        try:
            plan = frozen_plan(self.state.events(self.run_id))
            if plan is None:
                return _failure("FROZEN_PRODUCT_PLAN_REQUIRED")
            target = target_for(plan, self.run_id, binding["module"])
            expected = compile_binding(target, binding["pipeline_id"],
                                       self.api.compile_only_capability())
            if expected != binding:
                return _failure("BRANCH_COMPILE_CONFLICT")
            return ensure_compile(self, target, binding["pipeline_id"], approval)
        except (ValueError, KeyError) as error:
            return _failure(str(error))

    def _silent_case_failures(self, stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Jobs that reported success while their own log reports failed cases.

        The log also names the agent host and the script that ran, which is what a person
        needs to go and look at the environment with `tom-autodebug` when the numbers alone
        do not explain the failure.
        """
        found: list[dict[str, Any]] = []
        for stage in stages:
            jobs = [job for job in stage.get("jobs") or [] if job.get("status") in _SUCCESS]
            # The stage listing carries job statuses but no log links; only the stage detail
            # does. Without this lookup a stage bound from the listing has no readable log,
            # and a silent case failure under it stays invisible.
            missing = [job for job in jobs if not job.get("log_url")]
            resolved = (
                self._stage_job_logs(stage.get("stage_build_id")) if missing else {}
            )
            for job in jobs:
                log_url = job.get("log_url") or resolved.get(str(job.get("job_build_id") or ""), "")
                if not log_url:
                    continue
                parsed = self.log_reader(log_url)
                if not isinstance(parsed, dict) or not parsed.get("ok"):
                    continue
                ratios = [value for value in parsed.get("success_ratios") or [] if value < 100]
                failures = parsed.get("failed_cases") or [
                    case["case"] for case in parsed.get("case_results") or []
                    if isinstance(case, dict) and case.get("passed") is False
                ]
                if not ratios and not failures:
                    continue
                found.append({
                    "stage_build_id": stage.get("stage_build_id"),
                    "stage_name": stage.get("name"),
                    "job_build_id": job.get("job_build_id"),
                    "job_name": job.get("name"),
                    "log_url": log_url,
                    "success_ratios": parsed.get("success_ratios") or [],
                    "failed_cases": failures[:40],
                    # Where to look next, straight from the log.
                    "agent_host": parsed.get("agent_host"),
                    "agent_ip": parsed.get("agent_ip"),
                    "workspace": parsed.get("workspace"),
                    "scripts": parsed.get("scripts") or [],
                })
        return found

    def _mark_expression_skips(self, stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flag the wholly skipped stages the pipeline's own skip expression ruled out.

        The stage listing reports a skipped stage but not why; only the stage detail
        carries each job's `skipByStage`. A stage every one of whose jobs reports
        `EXPRESSION` was excluded by the pipeline configuration for this branch (BGW's
        348102 skips `P0新case回归` and `系统测试p0级功能回归` for every dpdk-2011 branch),
        so it can never run and is not missing evidence. A manual skip, an unreadable
        detail or any job without that marker leaves the stage unflagged.
        """
        for stage in stages:
            if stage.get("status") not in _SKIPPED or any(
                job not in _SKIPPED for job in stage.get("job_statuses") or []
            ):
                continue
            try:
                detail = self.api.stage_detail(str(stage.get("stage_build_id")))
            except Exception:
                continue
            reasons = _job_skip_reasons(detail)
            if reasons and all(reason == "EXPRESSION" for reason in reasons):
                stage["skipped_by_expression"] = True
        return stages

    def _stage_job_logs(self, stage_build_id: Any) -> dict[str, str]:
        """job_build_id -> log link, from the stage detail response.

        A missing or unreadable detail is not an error here: it only means this stage
        cannot contribute case evidence, which is what an empty mapping says.
        """
        if not stage_build_id:
            return {}
        try:
            detail = self.api.stage_detail(str(stage_build_id))
        except Exception:
            return {}
        found: dict[str, str] = {}
        pending: list[Any] = [detail]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                job_id = value.get("id") or value.get("jobBuildId")
                url = _job_log_url(value)
                if job_id and url:
                    found[str(job_id)] = url
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return found

    def stage_parameter_names(self, stage_build_id: str) -> dict[str, Any]:
        """Read the owned stage's declaration; absence of a declaration is not empty."""
        owned = self._load_stage_binding(stage_build_id)
        if owned is None:
            return _failure("STAGE_OWNERSHIP_UNVERIFIED")
        try:
            detail = self.api.stage_detail(stage_build_id)
        except Exception as error:
            return _failure(_transport_reason(error, "STAGE_PARAMETERS_QUERY_FAILED"))
        if isinstance(detail, dict) and "entities" in detail:
            detail = detail["entities"]
        stage = detail.get("stageBuild") if isinstance(detail, dict) else None
        if not isinstance(stage, dict):
            return _failure("STAGE_PARAMETERS_UNKNOWN")
        expected = {
            "stageBuildId": stage_build_id,
            "pipelineBuildId": owned["build_id"],
            "pipelineConfId": owned["context"]["pipeline_id"],
        }
        if any(str(stage.get(key)) != str(value) for key, value in expected.items()):
            return _failure("STAGE_PARAMETER_IDENTITY_MISMATCH")
        conf_id = owned["stage"].get("stage_conf_id")
        if conf_id and str(stage.get("stageConfId")) != str(conf_id):
            return _failure("STAGE_PARAMETER_IDENTITY_MISMATCH")
        from stage_parameters import declared_names, dependency_declaration, job_override_decision

        result = declared_names(stage)
        if hasattr(self.api, "stage_job_override_capability"):
            try:
                configuration = self.api.get_pipeline_by_id(
                    owned["context"]["pipeline_id"], brief=False)
            except Exception as error:
                return _failure(_transport_reason(error, "STAGE_PARAMETERS_QUERY_FAILED"))
            if str(configuration.get("id") or configuration.get("pipelineConfId")) != str(
                owned["context"]["pipeline_id"]
            ):
                return _failure("STAGE_PARAMETER_IDENTITY_MISMATCH")
            result = dependency_declaration(stage, detail, configuration)
            if result.get("ok") and result.get("requires_job_override"):
                # Same decision `_rerun_binding` applies to the write: an inherited or
                # job-level name cannot be carried by manual_execute_stage, so the G8
                # material is refused here instead of looking executable until the write.
                decision = job_override_decision(
                    {**result, "stage_name": stage.get("stageName", "")},
                    owned["stage"].get("stage_conf_id") or stage.get("stageConfId"),
                    getattr(self, "validated_profile", None) or {},
                    manual_names=declared_names(stage).get("names", []),
                    capability=self.api.stage_job_override_capability,
                )
                if "override_capability" in decision:
                    result["override_capability"] = decision["override_capability"]
                if not decision["executable"]:
                    result.update(
                        ok=False, reason_code="JOB_PARAMETER_OVERRIDE_UNSUPPORTED",
                        parameters=decision["job_level"] or decision["unknown"],
                        allowlisted_parameters=decision["allowlisted"],
                    )
        return {
            **result, "stage_build_id": stage_build_id,
            "stage_name": stage.get("stageName", ""),
            "source": "stageAndRealJobBuilds.stageBuild.manualParams",
        }

    def _rerun_binding(
        self, stage_build_id: str, parameters: dict[str, Any] | None
    ) -> dict[str, Any]:
        """What a re-run of this stage would decide, or why it cannot be decided.

        Both the approval request and the write itself need this, and they have to agree
        to the byte: an approval is only valid for the hash of exactly this binding.
        """
        from stage_parameters import redacted

        stage_binding = self._load_stage_binding(stage_build_id)
        if stage_binding is None:
            return _failure("STAGE_OWNERSHIP_UNVERIFIED")
        # A stage of a build this run replaced (ipipe-rebuild) or refused to adopt is
        # history: re-running it would produce evidence for a build the run no longer
        # follows. The new build's own stages are the ones to re-run.
        if self.is_excluded(str(stage_binding.get("build_id") or "")):
            return _failure(
                "STAGE_BUILD_SUPERSEDED", build_id=stage_binding.get("build_id"),
                stage_build_id=stage_build_id, retry_allowed=False,
            )
        if hasattr(self.api, "stage_job_override_capability"):
            declaration = self.stage_parameter_names(stage_build_id)
            if not declaration.get("ok"):
                return declaration
            if set(parameters or {}) != set(declaration["names"]):
                return _failure("STAGE_PARAMETER_NAMES_MISMATCH")
            from stage_parameters import project_mapping, product_command
            mapping = project_mapping(self.validated_profile or {}, declaration["stage_name"])
            proofs = {}
            for name in declaration["names"]:
                module = mapping.get(name)
                if not module or module == "test_cr_id":
                    continue
                proof = getattr(self, "_product_proofs", {}).get(module)
                if not proof or not proof.get("download_verified"):
                    return _failure("PRODUCT_PROOF_REQUIRED", parameter=name)
                value = redacted(parameters)[name]
                if value != product_command(proof["product_url"], "<IREPO-TOKEN>"):
                    return _failure("PRODUCT_PARAMETER_BINDING_MISMATCH", parameter=name)
                # URL equality is not enough: the proof's actual source revision must be
                # the revision this binding's repository list freezes for that module.
                bound_revisions = {
                    r.get("revision") for r in stage_binding["context"].get("repositories", [])
                    if isinstance(r, dict) and r.get("module") == module
                }
                frozen = proof.get("frozen_revision")
                if (not proof.get("revision") or (frozen and proof["revision"] != frozen)
                        or (bound_revisions and bound_revisions != {proof["revision"]})):
                    return _failure("PRODUCT_PARAMETER_BINDING_MISMATCH", parameter=name,
                                    reason="PRODUCT_REVISION_MISMATCH")
                proofs[module] = proof
            # A name declared on this very stage is filled through the stage's own
            # manual-execute form (`manual_execute_stage`). Only an inherited or
            # job-level declaration would need a job override, and no such write exists:
            # refuse here, before any G8 or intent, instead of failing after the claim.
            if declaration.get("requires_job_override"):
                from stage_parameters import job_override_decision
                decision = job_override_decision(
                    declaration, stage_binding["stage"].get("stage_conf_id"),
                    self.validated_profile or {},
                    manual_names=[n for n, entries in (declaration.get("declarations") or {}).items()
                                  if entries == [{"source": "manualParams"}]],
                    capability=self.api.stage_job_override_capability,
                )
                job_level = decision["job_level"]
                if not decision["executable"]:
                    return _failure("JOB_PARAMETER_OVERRIDE_UNSUPPORTED", parameters=job_level)
        else:
            declaration = None
            proofs = {}
        stage = stage_binding["stage"]
        rerunnable = stage["status"] in _FAILURE | _MANUAL
        # A stage whose jobs all exited zero while their logs report failed cases is a
        # failure the platform calls a success. Refusing to re-run it left the only remedy
        # outside the control plane, so the case evidence makes it rerunnable -- and the
        # approval is bound to that evidence, not merely to the stage.
        silent = [] if rerunnable else self._silent_case_failures([stage])
        if not rerunnable and not silent:
            return _failure("STAGE_NOT_RERUNNABLE")
        context = stage_binding["context"]
        build_id = stage_binding["build_id"]
        failure_signature = stage.get("failure_signature") or _failure_signature(
            context.get("pipeline_id"), context.get("module"), [stage], []
        )
        return {
            "ok": True,
            "reason_code": "OK",
            "context": context,
            "build_id": build_id,
            "stage": stage,
            "case_failures": silent,
            "binding": {
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
                **({"case_failure_signature": _canonical_hash(silent)} if silent else {}),
                **({"parameters": redacted(parameters)} if parameters else {}),
                **({"parameter_declaration": declaration} if declaration else {}),
                **({"product_proofs": proofs} if proofs else {}),
            },
        }

    def rerun_input_hash(
        self, stage_build_id: str, *, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The G8 hash to request approval for, without touching the stage."""
        bound = self._rerun_binding(stage_build_id, parameters)
        if not bound.get("ok"):
            return bound
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "stage_build_id": stage_build_id,
            "input_hash": _canonical_hash(bound["binding"]),
            "case_failures": bound["case_failures"],
            "binding": bound["binding"],
        }

    @guard_execution
    def rerun(
        self,
        stage_build_id: str,
        approval: dict[str, Any],
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-run or continue a stage, optionally carrying the inputs it asks for.

        A manual stage can declare parameters a person normally types. They are derived
        elsewhere (`stage_parameters`) and passed in here. What the G8 approval binds, and
        what the intent records, is the *redacted* form: the decision is "run this stage
        with this CR and this product", while the irepo token in the download command is a
        credential and has no business in an approval ledger or an audit payload.
        """
        from stage_parameters import redacted

        key = self._rerun_key(stage_build_id, approval)
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            response = completed["receipt"]["response"]
            if not _valid_replay_approval(
                self.approvals, approval, completed["intent"]["payload"], run_id=self.run_id
            ):
                return _failure("RERUN_CONFLICT")
            return dict(response)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
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

        stage_bound = self._rerun_binding(stage_build_id, parameters)
        if not stage_bound.get("ok"):
            return stage_bound
        binding = stage_bound["binding"]
        context = stage_bound["context"]
        build_id = stage_bound["build_id"]
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
        # A later legitimate re-run of the same stage is a new attempt under its own
        # approval, but only once every earlier attempt has a receipt. An earlier claim
        # without one is an unknown outcome: stacking another POST on it could run the
        # stage twice, so the only way forward is the GET-only reconcile.
        for prior in self._rerun_intents(stage_build_id, exclude_key=key):
            if self.state.result_by_idempotency_key(prior["idempotency_key"]) is None:
                return _failure(
                    "QUERY_REQUIRED",
                    intent_id=prior["intent_id"],
                    approval_id=prior["payload"].get("approval_id"),
                    retry_allowed=False,
                )
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        # Everything that can be checked without the platform is checked before the
        # claim: a programming error found after it would leave an intent that reads as
        # "outcome unknown" although nothing was ever sent.
        local = self._rerun_dispatch(stage_build_id, parameters)
        if local.get("ok") is False:
            return local
        dispatch, call_parameters = local["dispatch"], local["parameters"]
        # The loop above is an early answer; this claim is the authority. Checking for
        # an unreceipted attempt and inserting this one happen in one transaction, so two
        # concurrent G8s for the same stage cannot both get past the check and both POST.
        claim = self.state.claim_exclusive_intent(
            self.run_id, "ipipe.rerun", key, payload,
            exclusive_prefix=f"ipipe.rerun:{self.run_id}:{stage_build_id}",
        )
        if claim["status"] == "BLOCKED":
            return _failure(
                "QUERY_REQUIRED", intent_id=claim["intent"]["intent_id"],
                approval_id=(claim["intent"].get("payload") or {}).get("approval_id"),
                retry_allowed=False,
            )
        if claim["status"] != "CLAIMED":
            return _failure("RERUN_CONFLICT" if claim["status"] == "CONFLICT" else "QUERY_REQUIRED", retry_allowed=False)
        intent = claim["intent"]
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        try:
            response = dispatch(stage_build_id, call_parameters)
        except Exception as error:
            # What the platform said (or that it said nothing) is the only evidence of how
            # the write ended; without it the next session can only guess. Shape only.
            self._observe_write_error(intent["intent_id"], error)
            if _failed_before_dispatch(error):
                # Raised in this frame, before the client method ran: no request exists.
                # Close the claim on the record rather than leaving it unknown.
                try:
                    try:
                        self.state.abandon_intent(
                            intent["intent_id"],
                            f"RERUN_LOCAL_ERROR: {type(error).__name__} before any request was sent",
                            "system:ipipe_runtime",
                            local_error_proven=True,
                        )
                    except ValueError as proof_error:
                        if str(proof_error) != "LOCAL_ERROR_NOT_PROVEN":
                            raise
                        self.state.abandon_intent(
                            intent["intent_id"],
                            f"RERUN_LOCAL_ERROR: {type(error).__name__} before any request was sent",
                            "system:ipipe_runtime",
                        )
                except ValueError:
                    pass
                return _failure(
                    "RERUN_LOCAL_ERROR", intent_id=intent["intent_id"],
                    error_type=type(error).__name__, retry_allowed=False,
                )
            if isinstance(error, IpipeTransportError) and not error.transient:
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            reconciled = self._reconcile_rerun(intent)
            if reconciled is not None and reconciled.get("reason_code") in {"OK", "REVISION_MISMATCH"}:
                return reconciled
            return _failure("RERUN_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        response_stage = _rerun_response_stage(response)
        self.state.record_external_observation(intent["intent_id"], {
            "operation": "ipipe.rerun.response",
            "response_type": type(response).__name__,
            "response_keys": sorted(response) if isinstance(response, dict) else [],
            "stage_build_id": response_stage or None,
        })
        if not response_stage:
            # The manual-execute endpoint acknowledges with `{code, entities, msg}` and no
            # stage identity, and the stage listing reports the new start a moment later.
            # Reading it once, immediately, left every manual stage as "outcome unknown"
            # (BGW-1995, both pipelines), so the read-only confirmation is repeated a few
            # times before giving up; it never re-sends the write.
            for attempt in range(_RERUN_CONFIRM_POLLS):
                reconciled = self._reconcile_rerun(intent)
                if reconciled is not None:
                    return reconciled
                if attempt + 1 < _RERUN_CONFIRM_POLLS:
                    self.sleeper(_RERUN_CONFIRM_INTERVAL)
            return _failure("RERUN_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            current = self.api.build_by_id(build_id, **self._build_key(binding))
        except Exception as error:
            return _failure(
                _transport_reason(error, "RERUN_CONFIRMATION_REQUIRED"),
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        if not _matches_build(current, context):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            current_stages = _embedded_stages(current)
            if not current_stages:
                current_stages = self._build_stages(build_id, current)
        except Exception as error:
            return _failure(
                _transport_reason(error, "RERUN_CONFIRMATION_REQUIRED"),
                intent_id=intent["intent_id"], retry_allowed=False,
            )
        normalized = [_normalize_stage(item) for item in current_stages if isinstance(item, dict)]
        # Re-executing a stage that already finished makes iPipe allocate a *new* stage
        # build for the same stage of the same build, so the response names an id the
        # request never mentioned. Demanding the requested id back reported a started
        # stage as an invalid response; the successor is accepted only when it is the same
        # stage of the same build, and it is recorded so monitoring follows the new run.
        started = next(
            (item for item in normalized if item["stage_build_id"] == response_stage), None
        )
        if (
            started is None
            or not _rerun_started(started, since=intent.get("created_at"))
            or (started["stage_build_id"] != stage_build_id
                and started["stage_build_id"] in self._claimed_rerun_stages(intent["intent_id"]))
        ):
            return _failure("RERUN_CONFIRMATION_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        if started["stage_build_id"] != stage_build_id and not _same_stage(
            stage_bound["stage"], started
        ):
            return _failure("RERUN_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if started["stage_build_id"] != stage_build_id:
            self._bind_stage(build_id, context, started)
        return self._save_rerun_receipt(
            intent["intent_id"], payload, started_stage_build_id=started["stage_build_id"]
        )

    def _rerun_dispatch(
        self, stage_build_id: str, parameters: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The write callable and its arguments, or why this process cannot make the call.

        Resolved before the intent is claimed so a missing method or a malformed argument
        set never produces an intent with an unknown outcome.
        """
        dispatch = getattr(self.api, "manual_execute_stage", None)
        if not callable(dispatch):
            return _failure(
                "RERUN_LOCAL_ERROR", error_type="AttributeError", retry_allowed=False,
                detail="manual_execute_stage is not available on the iPipe adapter",
            )
        try:
            call_parameters = dict(parameters or {})
        except (TypeError, ValueError) as error:
            return _failure(
                "RERUN_LOCAL_ERROR", error_type=type(error).__name__, retry_allowed=False,
                detail="rerun parameters are not a mapping",
            )
        try:
            inspect.signature(dispatch).bind(stage_build_id, call_parameters)
        except TypeError as error:
            return _failure(
                "RERUN_LOCAL_ERROR", error_type=type(error).__name__, retry_allowed=False,
                detail="manual_execute_stage does not accept (stage_id, parameters)",
            )
        except ValueError:
            pass  # no introspectable signature; the call itself decides
        return {"ok": True, "dispatch": dispatch, "parameters": call_parameters}

    def _observe_write_error(
        self, intent_id: str, error: Exception, operation: str = "ipipe.rerun.write_error"
    ) -> None:
        """Persist the shape of a failed rerun write: status, type, field names, code text.

        Never the token, a signed URL, or a body; a failure to record must not change
        what the caller does with the failure itself.
        """
        observation: dict[str, Any] = {
            "operation": operation,
            "error_type": type(error).__name__,
            # Whether the error was raised by the call itself, before the client method
            # ran (so no request exists). `abandon-intent --local-error-proven` requires
            # this to be true; the type alone cannot tell a call-site error from one
            # raised inside the adapter after the request went out.
            "before_dispatch": _failed_before_dispatch(error),
        }
        if isinstance(error, IpipeTransportError):
            diagnostic = error.diagnostic or {}
            observation["reason_code"] = error.reason_code
            observation["transient"] = bool(error.transient)
            if isinstance(error.status, int):
                observation["http_status"] = error.status
            if isinstance(diagnostic.get("error_type"), str):
                observation["error_type"] = diagnostic["error_type"]
            for key in ("response_type", "body_chars"):
                if isinstance(diagnostic.get(key), (str, int)):
                    observation[key] = diagnostic[key]
            excerpt = diagnostic.get("body_excerpt")
            if isinstance(excerpt, str) and excerpt:
                observation.update(_body_shape(excerpt, truncated=bool(diagnostic.get("body_truncated"))))
        try:
            self.state.record_external_observation(intent_id, observation)
        except Exception:
            pass

    def _unstarted_verdict(
        self,
        intent: dict[str, Any],
        payload: dict[str, Any],
        requested: dict[str, Any],
        current_stage: dict[str, Any] | None,
        raw_stages: list[Any],
    ) -> dict[str, Any] | None:
        """Tell "still unknown" from "observed not started" for a claimed rerun.

        Reads can only ever show that a write landed. After a grace period an unchanged
        stage (same status, same start time, no successor run) is evidence the write did
        not take effect, but it is not proof the platform refused it: the request may
        still be queued. So the verdict is a distinct, non-retryable failure that makes
        the intent eligible for `abandon-intent`; it never authorises another POST.
        """
        if current_stage is None:
            return None
        if (
            current_stage["status"] != requested.get("status")
            or str(current_stage.get("start_time")) != str(requested.get("start_time"))
        ):
            return None
        try:
            created = datetime.fromisoformat(str(intent.get("created_at")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (self.clock() - created).total_seconds()
        except (ValueError, TypeError):
            return None
        if age < _RERUN_OBSERVATION_GRACE_SECONDS:
            return None
        raw = next(
            (
                item for item in raw_stages if isinstance(item, dict)
                and str(item.get("id") or item.get("stageBuildId") or _stage_id_from_jobs(item) or "")
                == payload["stage_build_id"]
            ),
            {},
        )
        evidence = {
            "stage_status": current_stage["status"],
            "stage_start_time": current_stage.get("start_time"),
            "stage_end_time": raw.get("endTime"),
            "successor_stage_found": False,
            "intent_age_seconds": int(age),
            "grace_seconds": _RERUN_OBSERVATION_GRACE_SECONDS,
        }
        try:
            self.state.record_external_observation(
                intent["intent_id"], {"operation": "ipipe.rerun.reconcile", **evidence}
            )
        except Exception:
            pass
        return _failure(
            "RERUN_NOT_STARTED_UNPROVEN",
            intent_id=intent["intent_id"],
            retry_allowed=False,
            abandon_eligible=True,
            rejection_proven=False,
            evidence=evidence,
        )

    def _save_rerun_receipt(
        self,
        intent_id: str,
        payload: dict[str, Any],
        *,
        started_stage_build_id: str | None = None,
    ) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        context = {
            "module": payload.get("module"),
            "revision_map": {
                item.get("module"): item.get("revision")
                for item in payload.get("repositories", [])
                if isinstance(item, dict) and item.get("module") and item.get("revision")
            },
        }
        started = started_stage_build_id or payload["stage_build_id"]
        stages = [{"stage_build_id": payload["stage_build_id"]}]
        if started != payload["stage_build_id"]:
            stages.append({"stage_build_id": started})
        evidence_refs = _build_evidence(payload["build_id"], context, stages)
        receipt = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "build_id": payload["build_id"],
            "stage_build_id": payload["stage_build_id"],
            "started_stage_build_id": started,
            "revision_set_id": payload["revision_set_id"],
            "evidence_refs": evidence_refs,
        }
        self.state.receipt(intent_id, receipt, receipt["evidence_refs"])
        return receipt

    def _rerun_key(self, stage_build_id: str, approval: Any) -> str:
        """Idempotency key of one authorised attempt on a stage.

        The key carries the approval id, so replaying the same authorisation stays
        idempotent while a new G8 for the same stage is a new attempt. The historic
        key (no approval dimension) keeps being honoured for the approval that
        created it; under any other approval it is merely a prior attempt.
        """
        legacy = f"ipipe.rerun:{self.run_id}:{stage_build_id}"
        approval_id = approval.get("approval_id") if isinstance(approval, dict) else None
        if not _nonempty(approval_id):
            return legacy
        old = self.state.intent_by_idempotency_key(legacy)
        if old is not None and old["payload"].get("approval_id") == approval_id:
            base = legacy
        else:
            base = f"{legacy}:{approval_id}"
        # One narrow re-issue under the same approval: the previous attempt was abandoned
        # with proof that a local programming error stopped it before any request existed.
        # The old intent stays as a finished prior attempt; the next key is `:retry-1`.
        key = base
        for attempt in range(1, _LOCAL_ERROR_RETRY_LIMIT + 1):
            if not self._abandoned_by_local_error(
                key, approval_id, next_key=f"{base}:retry-{attempt}"
            ):
                break
            key = f"{base}:retry-{attempt}"
        return key

    def _abandoned_by_local_error(
        self, key: str, approval_id: str, *, next_key: str | None = None
    ) -> bool:
        from state_store import _local_error_observed

        done = self.state.result_by_idempotency_key(key)
        if done is None:
            return False
        response = done["receipt"]["response"]
        intent = done["intent"]
        if (
            not isinstance(response, dict)
            or response.get("reason_code") != "INTENT_ABANDONED"
            or response.get("local_error_proven") is not True
            or intent["payload"].get("approval_id") != approval_id
        ):
            return False
        # The marker is not trusted on its own; the observation must still be on record.
        observations = self.state.external_observations(self.run_id, intent_id=intent["intent_id"])
        if _local_error_observed(intent, observations):
            return True
        # An abandonment receipted before observations carried `before_dispatch` keeps
        # pointing at the retry that was already made under it, so that history replays.
        # It never authorizes a retry that does not exist yet.
        return (
            next_key is not None
            and self.state.intent_by_idempotency_key(next_key) is not None
            and _local_error_observed(intent, observations, legacy=True)
        )

    def _rerun_intents(self, stage_build_id: str, *, exclude_key: str) -> list[dict[str, Any]]:
        """Every recorded rerun attempt on this stage except the one under `exclude_key`."""
        return [
            item for item in self.state.all_intents(self.run_id)
            if item["operation"] == "ipipe.rerun"
            and item["idempotency_key"] != exclude_key
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("stage_build_id") == stage_build_id
        ]

    @guard_execution
    def reconcile_rerun(self, stage_build_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        """Recover an already claimed write using GET only; never create an intent or POST."""
        key = self._rerun_key(stage_build_id, approval)
        intent = self.state.intent_by_idempotency_key(key)
        if intent is None:
            # Other attempts exist for this stage but none was claimed under this
            # approval: the approval does not match any recorded write.
            if self._rerun_intents(stage_build_id, exclude_key=key):
                return _failure("RERUN_CONFLICT", retry_allowed=False)
            return _failure("RERUN_INTENT_NOT_FOUND", retry_allowed=False)
        if not _valid_replay_approval(self.approvals, approval, intent["payload"], run_id=self.run_id):
            return _failure("RERUN_CONFLICT", retry_allowed=False)
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        return self._reconcile_rerun(intent) or _failure(
            "QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False
        )

    def _claimed_rerun_stages(self, intent_id: str) -> dict[str, Any]:
        """Stage builds other rerun intents of this run already receipted as started.

        Maps each stage build id to the latest receipt time (ISO string) that claimed it.
        """
        claimed: dict[str, Any] = {}
        for item in self.state.external_results(self.run_id):
            intent = item.get("intent") or {}
            if intent.get("operation") != "ipipe.rerun" or intent.get("intent_id") == intent_id:
                continue
            receipt = item.get("receipt") or {}
            response = receipt.get("response")
            if isinstance(response, dict) and response.get("ok") is True:
                started = response.get("started_stage_build_id")
                if isinstance(started, str) and started:
                    previous = claimed.get(started)
                    created = receipt.get("created_at")
                    if previous is None or (created and str(created) > str(previous)):
                        claimed[started] = created
        return claimed

    def _reconcile_rerun(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        payload = intent["payload"]
        try:
            build = self.api.build_by_id(payload["build_id"], **self._build_key(payload))
        except Exception as error:
            if isinstance(error, IpipeTransportError):
                return _failure(
                    error.reason_code, intent_id=intent["intent_id"], retry_allowed=False
                )
            return None
        # Identity is checked by the same rule the rest of this runtime uses: a build
        # record states the revision it was triggered on and only sometimes a map over
        # every repository, so an absent field is not evidence of a mismatch. Comparing a
        # whole revision map here made reconciliation impossible against the real gateway,
        # which returns no map at all.
        context = self._load_build_binding(payload["build_id"]) or {
            "pipeline_id": payload.get("pipeline_id"),
            "module": payload.get("module"),
            "revision_map": {
                item["module"]: item["revision"]
                for item in payload.get("repositories", [])
                if isinstance(item, dict)
                and _nonempty(item.get("module")) and _nonempty(item.get("revision"))
            },
            "parameters": (build or {}).get("params", (build or {}).get("parameters")),
        }
        if not _matches_build(build, context):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            stages = _embedded_stages(build)
            if not stages:
                stages = self._build_stages(payload["build_id"], build)
        except Exception:
            return None
        normalized = [_normalize_stage(item) for item in stages if isinstance(item, dict)]
        current_stage = next(
            (
                item for item in normalized
                if item["stage_build_id"] == payload["stage_build_id"]
            ),
            None,
        )
        # The requested stage itself may already be another attempt's run (a manual stage
        # continued in place keeps its id). It counts for this intent only when its start
        # is newer than the receipt that claimed it, i.e. a later execution.
        claimed_current = self._claimed_rerun_stages(intent["intent_id"]).get(
            payload["stage_build_id"], False)
        if (
            current_stage is not None
            and _rerun_started(current_stage, since=intent.get("created_at"))
            and (claimed_current is False
                 or _started_after(current_stage, claimed_current))
        ):
            return self._save_rerun_receipt(intent["intent_id"], payload)
        # The stage the intent names may have been superseded: re-executing a finished
        # stage allocates a new stage build, so the evidence that the write landed is a
        # sibling run of the same stage that is no longer waiting for a person.
        requested = self._stage_bindings.get(payload["stage_build_id"], {}).get("stage") or (
            self.state.idempotency_result(
                f"ipipe.stage-binding:{self.run_id}:{payload['stage_build_id']}"
            ) or {}
        ).get("stage")
        if not isinstance(requested, dict):
            return None
        # A successor already receipted by another rerun intent is that attempt's run,
        # not evidence for this one.
        claimed = self._claimed_rerun_stages(intent["intent_id"])
        successor = next(
            (
                item for item in normalized
                if item["stage_build_id"] != payload["stage_build_id"]
                and item["stage_build_id"] not in claimed
                and _same_stage(requested, item)
                and _rerun_started(item, since=intent.get("created_at"))
            ),
            None,
        )
        if successor is None:
            return self._unstarted_verdict(intent, payload, requested, current_stage, stages)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        binding = self._load_build_binding(payload["build_id"])
        if binding is not None:
            self._bind_stage(payload["build_id"], binding, successor)
        return self._save_rerun_receipt(
            intent["intent_id"], payload, started_stage_build_id=successor["stage_build_id"]
        )

    # ------------------------------------------------------------------
    # Controlled re-build (`ipipe-rebuild`)
    #
    # A build whose compile product can no longer be downloaded cannot host any stage
    # re-run, so the same revisions are built again. This is a second, separately
    # approved *trigger* of the pipeline, never a re-run, and it never moves ownership
    # by itself: the new build is recorded under its own key and the old one is only
    # marked superseded by `ipipe_rebuild.rebind`.
    # ------------------------------------------------------------------

    def is_superseded(self, build_id: str) -> bool:
        """Whether this run replaced `build_id` with a later build of the same revisions."""
        return build_markers.is_superseded(self.state, self.run_id, build_id)

    def is_excluded(self, build_id: str) -> bool:
        """Superseded by ipipe-rebuild or rejected after a mismatched trigger."""
        return build_markers.is_excluded(self.state, self.run_id, build_id)

    def _trigger_key_prefix(self, context: dict[str, Any]) -> str:
        return f"ipipe.trigger:{self.run_id}:{context['pipeline_id']}:{context['revision_set_id']}"

    def _rebuild_key(self, context: dict[str, Any], attempt: int) -> str:
        """Idempotency key of trigger attempt `attempt` (>= 2) for these revisions.

        The historic key (no attempt suffix) is attempt 1 and is left exactly as it is;
        a build the run only adopted is attempt 1 as well.
        """
        return f"{self._trigger_key_prefix(context)}:attempt-{int(attempt)}"

    def _trigger_intents(self, context: dict[str, Any], *, exclude_key: str = "") -> list[dict[str, Any]]:
        """Every recorded trigger attempt (any attempt number) for these revisions."""
        prefix = self._trigger_key_prefix(context)
        return [
            item for item in self.state.all_intents(self.run_id)
            if item["operation"] == "ipipe.trigger"
            and item["idempotency_key"] != exclude_key
            and (item["idempotency_key"] == prefix or item["idempotency_key"].startswith(f"{prefix}:attempt-"))
        ]

    def next_rebuild_attempt(self, context: dict[str, Any]) -> int:
        """The attempt number a fresh re-build would take: 2 after the original build."""
        prefix = f"{self._trigger_key_prefix(context)}:attempt-"
        highest = 1
        for item in self._trigger_intents(context):
            tail = item["idempotency_key"][len(prefix):] if item["idempotency_key"].startswith(prefix) else ""
            if tail.isdigit():
                highest = max(highest, int(tail))
        return highest + 1

    def _rebuilt_response(self, context: dict[str, Any], stale: dict[str, Any]) -> dict[str, Any]:
        """The receipt of the latest re-build when `stale` names a superseded build."""
        prefix = f"{self._trigger_key_prefix(context)}:attempt-"
        newest = None
        for item in self._trigger_intents(context):
            if not item["idempotency_key"].startswith(prefix):
                continue
            done = self.state.result_by_idempotency_key(item["idempotency_key"])
            response = done["receipt"]["response"] if done else None
            if (
                isinstance(response, dict) and response.get("ok") is True
                and not self.is_excluded(str(response.get("build_id") or ""))
            ):
                newest = response
        return dict(newest) if newest is not None else _failure(
            "TRIGGER_SUPERSEDED", build_id=stale.get("build_id"), retry_allowed=False
        )

    def rebuild_binding(
        self,
        profile: dict[str, Any],
        revision_set: dict[str, Any],
        module: Any,
        *,
        original_build_id: Any,
        attempt: Any,
        reason: Any,
        event_type: Any = "GERRIT_CHANGE",
    ) -> dict[str, Any]:
        """What one re-build approval authorizes; shared by the card, the hash and the write."""
        context = self._context(profile, revision_set, module)
        if context.get("reason_code") != "OK":
            return context
        forbidden = set(context["parameters"]) - set(context["allowed_parameters"])
        if forbidden or _contains_secret_material(context["parameters"]):
            return _failure("PIPELINE_PARAMETER_FORBIDDEN", forbidden_parameters=sorted(forbidden))
        text = " ".join(str(reason or "").split())
        if not text:
            return _failure("REBUILD_REASON_REQUIRED")
        if len(text) > _REBUILD_REASON_LIMIT or _contains_secret_material(text):
            return _failure("REBUILD_REASON_INVALID")
        if event_type is not None and event_type not in _REBUILD_EVENT_TYPES:
            return _failure("REBUILD_EVENT_TYPE_INVALID")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 2:
            return _failure("REBUILD_ATTEMPT_INVALID")
        original = str(original_build_id or "")
        if not original:
            return _failure("REBUILD_ORIGINAL_BUILD_REQUIRED")
        owned = self._load_build_binding(original)
        if (
            owned is None
            or owned.get("pipeline_id") != context["pipeline_id"]
            or owned.get("module") != context["module"]
            or owned.get("revision_set_id") != context["revision_set_id"]
        ):
            return _failure("BUILD_OWNERSHIP_UNVERIFIED", build_id=original)
        binding = {
            "operation": "ipipe.rebuild",
            "run_id": self.run_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "revision_set_id": context["revision_set_id"],
            "repositories": context["repositories"],
            "environment_fingerprint": context["environment_fingerprint"],
            "parameters": context["parameters"],
            "original_build_id": original,
            "attempt": attempt,
            "event_type": event_type,
            "reason": text,
        }
        return {
            "ok": True, "reason_code": "OK", "run_id": self.run_id, "context": context,
            "binding": binding, "input_hash": _canonical_hash(binding),
        }

    def rebuild_state(
        self, profile: dict[str, Any], revision_set: dict[str, Any], module: Any = None
    ) -> dict[str, Any]:
        """Read-only: the next attempt number and any trigger intent of unknown outcome."""
        context = self._context(profile, revision_set, module)
        if context.get("reason_code") != "OK":
            return context
        pending = [
            item["intent_id"] for item in self._trigger_intents(context)
            if self.state.result_by_idempotency_key(item["idempotency_key"]) is None
        ]
        return {"ok": True, "reason_code": "OK", "next_attempt": self.next_rebuild_attempt(context),
                "pending": pending}

    def build_ci_type(self, build_id: str) -> str | None:
        """The `trigger.ciType` the platform records for an owned build, if it says."""
        binding = self._load_build_binding(build_id)
        if binding is None:
            return None
        try:
            build = self.api.build_by_id(build_id, **self._build_key(binding))
        except Exception:  # noqa: BLE001 - informational only
            return None
        trigger = build.get("trigger") if isinstance(build, dict) else None
        value = trigger.get("ciType") if isinstance(trigger, dict) else None
        return value if isinstance(value, str) and value else None

    def rebuild_input_hash(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """The G8 hash to request for a re-build, without touching the pipeline."""
        bound = self.rebuild_binding(*args, **kwargs)
        if not bound.get("ok"):
            return bound
        return {key: bound[key] for key in ("ok", "reason_code", "run_id", "input_hash", "binding")}

    def _rebuild_dispatch(self, event_type: Any) -> dict[str, Any]:
        """The trigger callable and its keyword arguments, resolved before any claim."""
        dispatch = getattr(self.api, "trigger_by_revision", None)
        if not callable(dispatch):
            return _failure(
                "REBUILD_LOCAL_ERROR", error_type="AttributeError", retry_allowed=False,
                detail="trigger_by_revision is not available on the iPipe adapter",
            )
        extra = {"event_type": event_type} if event_type is not None else {}
        try:
            inspect.signature(dispatch).bind("0", "r", {}, **extra)
        except TypeError as error:
            return _failure(
                "REBUILD_LOCAL_ERROR", error_type=type(error).__name__, retry_allowed=False,
                detail="trigger_by_revision does not accept (pipeline_id, revision, parameters, event_type)",
            )
        except ValueError:
            pass
        return {"ok": True, "dispatch": dispatch, "kwargs": extra}

    def _rebuild_candidates(
        self, payload: dict[str, Any], binding: dict[str, Any], *, since: Any = None
    ) -> list[dict[str, Any]]:
        """GET only: builds of exactly these revisions that are newer than the original
        and not already known to this run (an earlier attempt, or a superseded build)."""
        original = str(payload["original_build_id"])
        listing = self.api.builds_by_revision(
            binding["module"], binding["revision_map"][binding["module"]], binding["pipeline_id"]
        )
        found: dict[str, dict[str, Any]] = {}
        for item in listing:
            identity = _build_id(item)
            if not identity or identity == original or identity in found:
                continue
            if not _matches_build(item, binding) or self.is_excluded(identity):
                continue
            if self._load_build_binding(identity) is not None:
                continue
            if identity.isdigit() and original.isdigit() and int(identity) <= int(original):
                continue
            if since is not None and _started_before(item, since, _REBUILD_CLOCK_SKEW_SECONDS):
                continue
            found[identity] = item
        return list(found.values())

    @guard_execution
    def rebuild(
        self,
        profile: dict[str, Any],
        revision_set: dict[str, Any],
        approval: dict[str, Any],
        *,
        module: Any,
        original_build_id: Any,
        attempt: Any,
        reason: Any,
        event_type: Any = "GERRIT_CHANGE",
    ) -> dict[str, Any]:
        """Trigger the pipeline again for the same revisions under one G8, at most once.

        Same discipline as `trigger` and `rerun`: the approval is bound to the exact
        binding, the intent is claimed before the POST, and anything unclear afterwards
        is resolved with GET only. A response that names no build is never guessed at.
        """
        bound = self.rebuild_binding(
            profile, revision_set, module, original_build_id=original_build_id,
            attempt=attempt, reason=reason, event_type=event_type,
        )
        if not bound.get("ok"):
            return bound
        binding, input_hash, context = bound["binding"], bound["input_hash"], bound["context"]
        failure = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G8", input_hash=input_hash
        )
        if failure is not None:
            return failure
        payload = {**binding, "input_hash": input_hash, "approval_id": approval["approval_id"]}
        key = self._rebuild_key(context, binding["attempt"])
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            if completed["intent"]["payload"] != payload:
                return _failure("REBUILD_CONFLICT")
            return dict(completed["receipt"]["response"])
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        existing = self.state.intent_by_idempotency_key(key)
        if existing is not None:
            if existing["payload"] != payload:
                return _failure("REBUILD_CONFLICT")
            return self._reconcile_rebuild(existing) or _failure(
                "QUERY_REQUIRED", intent_id=existing["intent_id"], retry_allowed=False
            )
        # Fresh attempt: it has to be the next one, on a build that is still current, and
        # nothing earlier may be of unknown outcome (a second POST on top of it could
        # build the revisions twice).
        expected = self.next_rebuild_attempt(context)
        if binding["attempt"] != expected:
            return _failure("REBUILD_ATTEMPT_STALE", expected_attempt=expected, retry_allowed=False)
        if self.is_superseded(binding["original_build_id"]):
            return _failure("REBUILD_ORIGINAL_SUPERSEDED", retry_allowed=False)
        for prior in self._trigger_intents(context, exclude_key=key):
            if self.state.result_by_idempotency_key(prior["idempotency_key"]) is None:
                return _failure(
                    "QUERY_REQUIRED", intent_id=prior["intent_id"],
                    approval_id=(prior.get("payload") or {}).get("approval_id"), retry_allowed=False,
                )
        local = self._rebuild_dispatch(binding["event_type"])
        if local.get("ok") is False:
            return local
        owned = self._load_build_binding(binding["original_build_id"]) or {}
        try:
            already = self._rebuild_candidates(binding, owned)
        except Exception as error:
            return _failure(_transport_reason(error, "BUILD_QUERY_FAILED"), retry_allowed=False)
        if len(already) > 1:
            return _failure("BUILD_AMBIGUOUS", retry_allowed=False,
                            candidates=sorted(_build_id(item) for item in already))
        claim = self.state.claim_intent(self.run_id, "ipipe.trigger", key, payload)
        if claim["status"] != "CLAIMED":
            return _failure("REBUILD_CONFLICT" if claim["status"] == "CONFLICT" else "QUERY_REQUIRED",
                            retry_allowed=False)
        intent = claim["intent"]
        if already:
            # A newer build of these exact revisions already exists (started from the
            # iPipe page, say). Following it is the re-build; a third build would not be.
            return self._rebuild_receipt(intent["intent_id"], payload, owned, already[0], adopted=True)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        try:
            result = local["dispatch"](
                binding["pipeline_id"], context["revision_map"][binding["module"]],
                dict(binding["parameters"]), **local["kwargs"],
            )
        except Exception as error:
            self._observe_write_error(intent["intent_id"], error, "ipipe.rebuild.write_error")
            if _failed_before_dispatch(error):
                # No request exists, so the claim is closed on the record (the trigger
                # intent would otherwise stay pending and every later plan would answer
                # QUERY_REQUIRED for good). If the proof cannot be recorded, say so: a
                # silently ignored failure here was exactly how the intent got stuck.
                try:
                    self.state.abandon_intent(
                        intent["intent_id"],
                        f"REBUILD_LOCAL_ERROR: {type(error).__name__} before any request was sent",
                        "system:ipipe_runtime", local_error_proven=True,
                    )
                except ValueError as abandon_error:
                    return _failure(
                        "REBUILD_LOCAL_ERROR_UNRECORDED", intent_id=intent["intent_id"],
                        error_type=type(error).__name__, abandon_error=str(abandon_error),
                        retry_allowed=False,
                        detail="the trigger intent is still open; reconcile or abandon-intent it",
                    )
                return _failure("REBUILD_LOCAL_ERROR", intent_id=intent["intent_id"],
                                error_type=type(error).__name__, retry_allowed=False,
                                intent_closed=True)
            if isinstance(error, IpipeTransportError) and not error.transient:
                return _failure(error.reason_code, intent_id=intent["intent_id"], retry_allowed=False)
            reconciled = self._reconcile_rebuild(intent)
            if reconciled is not None and reconciled.get("ok"):
                return reconciled
            return _failure("REBUILD_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        build_id = ""
        if isinstance(result, dict):
            build_id = str(result.get("id") or result.get("pipelineBuildId") or "")
        # Shape only: never the body, an error text or a URL.
        self.state.record_external_observation(intent["intent_id"], {
            "operation": "ipipe.rebuild.response",
            "response_type": type(result).__name__,
            "response_keys": sorted(result) if isinstance(result, dict) else [],
            "success": result.get("success") if isinstance(result, dict) and isinstance(result.get("success"), bool) else None,
            "limited": result.get("limited") if isinstance(result, dict) and isinstance(result.get("limited"), bool) else None,
            "build_id": build_id or None,
        })
        if not build_id:
            reconciled = self._reconcile_rebuild(intent)
            if reconciled is not None:
                return reconciled
            return _failure("REBUILD_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        if build_id == binding["original_build_id"] or self._load_build_binding(build_id) is not None:
            return _failure("REBUILD_RESPONSE_INVALID", intent_id=intent["intent_id"], retry_allowed=False)
        try:
            candidate = self.api.build_by_id(build_id, **self._build_key(owned))
        except Exception as error:
            return _failure(_transport_reason(error, "REBUILD_CONFIRMATION_REQUIRED"),
                            intent_id=intent["intent_id"], retry_allowed=False)
        if not _matches_build(candidate, owned):
            return _failure("REVISION_MISMATCH", intent_id=intent["intent_id"], retry_allowed=False)
        return self._rebuild_receipt(intent["intent_id"], payload, owned, candidate)

    @guard_execution
    def reconcile_rebuild(
        self,
        profile: dict[str, Any],
        revision_set: dict[str, Any],
        approval: dict[str, Any],
        *,
        module: Any,
        original_build_id: Any,
        attempt: Any,
        reason: Any,
        event_type: Any = "GERRIT_CHANGE",
    ) -> dict[str, Any]:
        """Settle an already claimed re-build using GET only; never creates an intent or POSTs."""
        bound = self.rebuild_binding(
            profile, revision_set, module, original_build_id=original_build_id,
            attempt=attempt, reason=reason, event_type=event_type,
        )
        if not bound.get("ok"):
            return bound
        failure = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G8", input_hash=bound["input_hash"]
        )
        if failure is not None:
            return failure
        key = self._rebuild_key(bound["context"], bound["binding"]["attempt"])
        payload = {**bound["binding"], "input_hash": bound["input_hash"],
                   "approval_id": approval["approval_id"]}
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            if completed["intent"]["payload"] != payload:
                return _failure("REBUILD_CONFLICT", retry_allowed=False)
            return dict(completed["receipt"]["response"])
        intent = self.state.intent_by_idempotency_key(key)
        if intent is None:
            return _failure("REBUILD_INTENT_NOT_FOUND", retry_allowed=False)
        if intent["payload"] != payload:
            return _failure("REBUILD_CONFLICT", retry_allowed=False)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        return self._reconcile_rebuild(intent) or _failure(
            "QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False
        )

    def _reconcile_rebuild(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        """The one build this intent started, found by revision and time; never guessed.

        Only a build newer than the original, for exactly the approved revisions and
        pipeline, that this run does not already own can be claimed. Two such builds are
        BUILD_AMBIGUOUS; none leaves the intent open for a later GET.
        """
        payload = intent["payload"]
        owned = self._load_build_binding(str(payload.get("original_build_id") or ""))
        if owned is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED", intent_id=intent["intent_id"],
                            retry_allowed=False)
        try:
            candidates = self._rebuild_candidates(payload, owned, since=intent.get("created_at"))
        except Exception as error:
            if isinstance(error, IpipeTransportError):
                return _failure(error.reason_code, intent_id=intent["intent_id"], retry_allowed=False)
            return None
        if len(candidates) > 1:
            return _failure("BUILD_AMBIGUOUS", intent_id=intent["intent_id"], retry_allowed=False,
                            candidates=sorted(_build_id(item) for item in candidates))
        if not candidates:
            return None
        return self._rebuild_receipt(intent["intent_id"], payload, owned, candidates[0])

    def _rebuild_receipt(
        self, intent_id: str, payload: dict[str, Any], owned: dict[str, Any],
        build: dict[str, Any], *, adopted: bool = False,
    ) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if not _matches_build(build, owned):
            return _failure("REVISION_MISMATCH", intent_id=intent_id, retry_allowed=False)
        build_id = _build_id(build)
        if not build_id:
            return _failure("REBUILD_RESPONSE_INVALID", intent_id=intent_id, retry_allowed=False)
        self._bind_build(build_id, owned, build)
        ci_type = (build.get("trigger") or {}).get("ciType") if isinstance(build.get("trigger"), dict) else None
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "build_id": build_id,
            "pipeline_id": payload["pipeline_id"],
            "module": payload["module"],
            "revision_set_id": payload["revision_set_id"],
            "parameters": payload["parameters"],
            "environment_fingerprint": payload["environment_fingerprint"],
            "rebuild": True,
            "attempt": payload["attempt"],
            "original_build_id": payload["original_build_id"],
            "approval_id": payload["approval_id"],
            "adopted": adopted,
            "ci_type": ci_type if isinstance(ci_type, str) else None,
            "evidence_refs": _build_evidence(build_id, owned),
        }
        self.state.receipt(intent_id, response, response["evidence_refs"])
        return response

    def bind_stages(self, build_id: str) -> dict[str, Any]:
        """Record stage ownership for an owned build with GET only (what `monitor` does
        on its first pass, without judging the build). A later G8 for any of its stages
        refuses with STAGE_OWNERSHIP_UNVERIFIED unless this has run."""
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED", build_id=build_id)
        try:
            build = self.api.build_by_id(build_id, **self._build_key(binding))
            stages = [_normalize_stage(stage) for stage in self._build_stages(build_id, build)]
        except Exception as error:
            return _failure(_transport_reason(error, "STAGE_QUERY_FAILED"))
        if _build_id(build) != build_id:
            return _failure("BUILD_IDENTITY_MISMATCH")
        if not _matches_build(build, binding):
            return _failure("REVISION_MISMATCH")
        identity_failure = _stage_identity_failure(stages)
        if identity_failure is not None:
            return _failure(identity_failure)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        for stage in stages:
            self._bind_stage(build_id, binding, stage)
        return {
            "ok": True, "reason_code": "OK", "build_id": build_id,
            "status": _status(build),
            "stages": [
                {"stage_build_id": stage["stage_build_id"], "name": stage["name"],
                 "status": stage["status"]} for stage in stages
            ],
        }

    def verify_release(self, build_id: str, revision_set: dict[str, Any]) -> dict[str, Any]:
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        if revision_set.get("run_id") != self.run_id or revision_set.get("revision_set_id") != binding["revision_set_id"]:
            return _failure("REVISION_MISMATCH")
        if revision_set.get("repositories") != binding["repositories"]:
            return _failure("REVISION_MISMATCH")
        try:
            current = self.api.build_by_id(build_id, **self._build_key(binding))
        except Exception as error:
            return _failure(_transport_reason(error, "BUILD_QUERY_FAILED"))
        if _build_id(current) != build_id:
            return _failure("BUILD_IDENTITY_MISMATCH")
        if not _matches_build(current, binding):
            return _failure("REVISION_MISMATCH")
        if _status(current) not in _SUCCESS | _SKIPPED:
            return _failure("PIPELINE_NOT_SUCCESSFUL")
        try:
            stages = [_normalize_stage(stage) for stage in self._build_stages(build_id, current)]
        except Exception as error:
            return _failure(_transport_reason(error, "STAGE_QUERY_FAILED"))
        stage_identity_failure = _stage_identity_failure(stages)
        if stage_identity_failure is not None:
            return _failure(stage_identity_failure)
        if not _build_passed(_status(current), self._mark_expression_skips(stages)):
            return _failure("PIPELINE_NOT_SUCCESSFUL")
        try:
            branch = self._branch_release(binding)
        except Exception as error:
            return _failure(_transport_reason(error, "RELEASE_QUERY_FAILED"))
        if branch.get("reason_code") != "BRANCH_PIPELINE_UNSUPPORTED":
            if not branch.get("ok"):
                return branch
            refs = _build_evidence(build_id, binding) + [
                f"ipipe:branch-build/{branch['branch_build_id']}",
                f"ipipe:release/{branch['release_id']}",
            ]
            return {
                "ok": True, "reason_code": "OK", "status": "SUCCESS", "run_id": self.run_id,
                "build_id": build_id, "release_id": branch["release_id"],
                "release_version": branch.get("version"),
                "release_product_url": branch.get("product_url"),
                "revision_set_id": binding["revision_set_id"],
                "release_rule": binding["release_rule"],
                "environment_fingerprint": binding["environment_fingerprint"],
                "evidence_refs": refs,
            }
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

    def branch_release_build(self, binding: dict[str, Any]) -> dict[str, Any]:
        """The target branch's BranchPipeline build of exactly this revision, if one exists.

        A merged CR is released by the module's BranchPipeline, not by the ChangePipeline
        that verified it, so the release is looked for there. `BRANCH_PIPELINE_UNSUPPORTED`
        means this adapter cannot name that pipeline and the legacy release listing applies.
        """
        lookup = getattr(self.api, "pipelines_by_module", None)
        if not callable(lookup):
            return _failure("BRANCH_PIPELINE_UNSUPPORTED")
        module = binding["module"]
        revision = (binding.get("revision_map") or {}).get(module)
        ids = {
            str(item.get("id") or item.get("pipelineConfId") or "")
            for item in lookup(module) or []
            if isinstance(item, dict)
            and (item.get("pipelineName") or item.get("name")) == "BranchPipeline"
        }
        if len(ids) != 1 or "" in ids or not _nonempty(revision):
            return _failure("BRANCH_PIPELINE_UNSUPPORTED")
        pipeline_id = next(iter(ids))
        builds = [
            item for item in self.api.builds_by_revision(module, revision, pipeline_id) or []
            if isinstance(item, dict)
            and str(item.get("branch") or (item.get("trigger") or {}).get("branch") or "")
            == binding["target_branch"]
            and str((item.get("trigger") or {}).get("revision") or item.get("revision") or "")
            == revision
        ]
        if not builds:
            return _failure("RELEASE_WAITING", status="RELEASE_WAITING",
                            branch_pipeline_id=pipeline_id, branch_build_id=None)
        # The newest build of this exact revision: iPipe build ids are increasing numbers.
        build = max(builds, key=lambda item: (len(_build_id(item)), _build_id(item)))
        return {"ok": True, "reason_code": "OK", "branch_pipeline_id": pipeline_id,
                "branch_build_id": _build_id(build), "build": build,
                "context": {**binding, "pipeline_id": pipeline_id}}

    def _branch_release(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Read the release from the BranchPipeline's own AGILE_RELEASE job.

        `/api/agile/getReleaseInfo` answers 404 on the real gateway for every branch; the
        release stage's job detail carries the version, source revision, branch and the
        product URL, so that is the evidence read here. No exact successful release job
        means the release has not happened yet (`RELEASE_WAITING`), with the branch build
        named so the release path can advance its manual stages.
        """
        found = self.branch_release_build(binding)
        if not found.get("ok"):
            return found
        module = binding["module"]
        revision = binding["revision_map"][module]
        stages = [_normalize_stage(stage)
                  for stage in self._build_stages(found["branch_build_id"], found["build"])]
        for stage in stages:
            if stage.get("status") not in _SUCCESS:
                continue
            detail = self.api.stage_detail(str(stage["stage_build_id"]))
            for job in _real_jobs(detail):
                real = job.get("realJobBuild") if isinstance(job.get("realJobBuild"), dict) else job
                if (job.get("jobType") == "AGILE_RELEASE"
                        and str(job.get("status") or "").upper() in _SUCCESS
                        and real.get("revision") == revision
                        and real.get("branch") == binding["target_branch"]
                        and _nonempty(real.get("version"))):
                    return {"ok": True, "reason_code": "OK",
                            "release_id": str(real.get("id") or real["version"]),
                            "version": real["version"],
                            "product_url": real.get("releaseOutputHttpUrl"),
                            "branch_build_id": found["branch_build_id"],
                            "branch_pipeline_id": found["branch_pipeline_id"]}
        return _failure("RELEASE_WAITING", status="RELEASE_WAITING",
                        branch_pipeline_id=found["branch_pipeline_id"],
                        branch_build_id=found["branch_build_id"])

    def bind_branch_release_build(self, build_id: str) -> dict[str, Any]:
        """Own the BranchPipeline build that releases this ChangePipeline build's revision.

        Its stages are then visible to the ordinary manual-stage path (parameter
        discovery, G8, rerun, reconcile) exactly like a ChangePipeline stage.
        """
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        found = self.branch_release_build(binding)
        if not found.get("ok"):
            return found
        self._bind_build(found["branch_build_id"], found["context"], found["build"])
        return {"ok": True, "reason_code": "OK", "branch_build_id": found["branch_build_id"],
                "branch_pipeline_id": found["branch_pipeline_id"]}

    def verify_release_of_build(self, build_id: str) -> dict[str, Any]:
        """Verify a build against its OWN recorded binding — per-module independent release
        (R4-H2). A required module releases the build that passed its pipeline, verified on that
        build's own revision set, not on the current whole-repo set (which a sibling module's
        later change would invalidate, leaving an unchanged module's valid build stuck). Reuses
        verify_release's platform checks by feeding it the build's own revision identity."""
        binding = self._load_build_binding(build_id)
        if binding is None:
            return _failure("BUILD_OWNERSHIP_UNVERIFIED")
        revision_set = {
            "run_id": self.run_id,
            "revision_set_id": binding.get("revision_set_id"),
            "repositories": binding.get("repositories"),
        }
        return self.verify_release(build_id, revision_set)

    @guard_execution
    def verify_planned_release(self, build_id: str, target: dict[str, Any]) -> dict[str, Any]:
        """Verify the current frozen target before checking the build's platform release."""
        from pipeline_plan import build_matches, frozen_plan, verification_key, canonical_hash
        plan = frozen_plan(self.state.events(self.run_id))
        if (plan is None or not isinstance(target, dict)
                or plan["modules"].get(target.get("module")) != target):
            return _failure("PIPELINE_PLAN_MISMATCH")
        if plan.get("profile_content_hash") != canonical_hash(self.validated_profile):
            return _failure("PIPELINE_PLAN_PROFILE_MISMATCH")
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        binding = self._load_build_binding(build_id)
        if not build_matches(binding, target):
            return _failure("BUILD_BINDING_MISMATCH")
        verified = self.verify_release_of_build(build_id)
        if not verified.get("ok"):
            return verified
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        proof = {
            "pipeline_id": target["pipeline_id"], "module": target["module"],
            "build_id": build_id, "release_id": verified["release_id"],
            "revisions": target["source_revisions"],
            "environment_fingerprint": target["environment_fingerprint"],
            "release_rule": target["release_rule"], "status": "SUCCESS",
            "release_evidence": verified["evidence_refs"],
            "remote_evidence_refs": verified["evidence_refs"],
        }
        self.state.save_idempotency_result(
            verification_key(self.run_id, build_id, target, verified["release_id"]), proof)
        return {**verified, "proof": proof}

    def _trigger_receipt(self, intent_id: str, context: dict[str, Any], build: dict[str, Any]) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
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

    def _context(
        self, profile: dict[str, Any], revision_set: dict[str, Any], module: Any = None
    ) -> dict[str, Any]:
        if not isinstance(self.validated_profile, dict) or not _nonempty(self.profile_hash):
            return _failure("PROFILE_BINDING_REQUIRED")
        if (profile != self.validated_profile
                or self.profile_hash != self._bound_profile_hash
                or _canonical_hash(self.validated_profile) != self._profile_content_hash):
            return _failure("PROFILE_CONFLICT")
        validation = validate_profile(profile, check_paths=False)
        if not validation.get("ready"):
            return _failure("PROJECT_NOT_READY")
        context = _context(profile, revision_set, self.run_id, module)
        if context.get("reason_code") == "OK":
            context["profile_hash"] = self.profile_hash
        return context

    def _profile_error(self) -> dict[str, Any] | None:
        """Validate the current pin before fresh effects by a cached runtime.

        An approved re-pin invalidates old instances; the factory must construct a
        new one. Legacy, directly constructed clients without a recorded profile
        path retain their in-memory binding checks, but cannot claim disk coverage.
        """
        if not isinstance(self.validated_profile, dict) or not _nonempty(self.profile_hash):
            return _failure("PROFILE_BINDING_REQUIRED")
        if (self.profile_hash != self._bound_profile_hash
                or _canonical_hash(self.validated_profile) != self._profile_content_hash):
            return _failure("PROFILE_CONFLICT")
        events = self.state.events(self.run_id)
        intake = events[0].get("payload") if events else None
        if not isinstance(intake, dict) or "profile_path" not in intake:
            return None
        path = intake.get("profile_path")
        expected = pinned_hash(events, record_for(self.state, self.run_id))
        if not _nonempty(path) or not _nonempty(expected):
            return _failure("PROJECT_NOT_READY")
        if expected != self._bound_profile_hash:
            return _failure("PROFILE_CONFLICT")
        try:
            raw = Path(path).read_bytes()
        except OSError:
            return _failure("PROJECT_NOT_READY")
        if hashlib.sha256(raw).hexdigest() != expected:
            return _failure("PROFILE_CONFLICT")
        try:
            current = yaml.safe_load(raw)
        except (UnicodeDecodeError, yaml.YAMLError):
            return _failure("PROJECT_NOT_READY")
        if current != self.validated_profile:
            return _failure("PROFILE_CONFLICT")
        return None

    def _build_stages(self, build_id: str, build: dict[str, Any]) -> list[dict[str, Any]]:
        """The build's stages, preferring the stage endpoint and falling back to the
        stages the build record already embeds.

        A failed stage query is only recoverable when the build record itself names
        stages; otherwise the typed failure is re-raised so its reason survives.
        """
        try:
            stages = self.api.pipeline_stage_info(build_id)
        except (KeyError, TypeError, ValueError):
            embedded = _embedded_stages(build)
            if not embedded:
                raise
            stages = embedded
        except IpipeTransportError as error:
            if error.status not in {404, 405} and error.reason_code not in {
                "STAGE_ENDPOINT_UNSUPPORTED", "OBJECT_NOT_FOUND",
            }:
                raise
            embedded = _embedded_stages(build)
            if not embedded:
                raise
            stages = embedded
        if not stages:
            stages = _embedded_stages(build)
        return [stage for stage in stages if isinstance(stage, dict)]

    def _build_key(self, source: Any) -> dict[str, Any]:
        """The module, revision and pipeline a build record is looked up by.

        The gateway has no per-build resource, so reading one build means selecting it
        out of the listing for its own revision; every binding this runtime writes
        already carries that revision, either mapped or as a repository list.
        """
        if not isinstance(source, dict):
            return {"module": "", "revision": "", "pipeline_id": ""}
        module = source.get("module")
        revision = (source.get("revision_map") or {}).get(module)
        if not _nonempty(revision):
            for repository in source.get("repositories") or []:
                if isinstance(repository, dict) and repository.get("module") == module:
                    revision = repository.get("revision")
                    break
        return {
            "module": str(module or ""),
            "revision": str(revision or ""),
            "pipeline_id": str(source.get("pipeline_id") or ""),
        }

    def _bind_build(self, build_id: str, context: dict[str, Any], build: dict[str, Any]) -> None:
        binding = {
            key: context[key]
            for key in (
                "pipeline_id", "module", "revision_set_id", "repositories", "revision_map",
                "environment_fingerprint", "parameters", "target_branch", "release_rule", "stage_classes",
            )
        }
        try:
            self.state.save_idempotency_result(
                f"ipipe.build-binding:{self.run_id}:{build_id}",
                {"run_id": self.run_id, "build_id": build_id, "binding": binding},
            )
        except ValueError:
            # Ownership of a build is written once and never moves. Discovering the same
            # build again -- a second adopt, a watcher tick after a diagnosis -- must
            # therefore keep the stored binding rather than overwrite or fail.
            stored = self.state.idempotency_result(f"ipipe.build-binding:{self.run_id}:{build_id}")
            if isinstance(stored, dict) and isinstance(stored.get("binding"), dict):
                self._build_bindings[build_id] = stored["binding"]
                return
        self._build_bindings[build_id] = binding

    def _bind_stage(self, build_id: str, context: dict[str, Any], stage: dict[str, Any]) -> None:
        stage_build_id = stage.get("stage_build_id")
        if not isinstance(stage_build_id, str) or not stage_build_id:
            return
        durable = {
            "run_id": self.run_id,
            "stage_build_id": stage_build_id,
            "build_id": build_id,
            "context": context,
            "stage": stage,
        }
        try:
            self.state.save_idempotency_result(
                f"ipipe.stage-binding:{self.run_id}:{stage_build_id}", durable
            )
        except ValueError:
            # The immutable binding is already owned by this run. Status changes are
            # recorded separately by _failure_evidence; ownership itself must not move.
            pass
        self._stage_bindings[stage_build_id] = {**durable, "stage": stage}

    def _load_stage_binding(self, stage_build_id: str) -> dict[str, Any] | None:
        binding = self._stage_bindings.get(stage_build_id)
        if binding is None:
            binding = self.state.idempotency_result(
                f"ipipe.stage-binding:{self.run_id}:{stage_build_id}"
            )
            if (
                not isinstance(binding, dict)
                or binding.get("run_id") != self.run_id
                or binding.get("stage_build_id") != stage_build_id
                or not isinstance(binding.get("context"), dict)
            ):
                return None
            stage = binding.get("stage")
            if not isinstance(stage, dict):
                return None
            failure = self.state.idempotency_result(
                f"ipipe.stage-failure:{self.run_id}:{stage_build_id}"
            )
            if isinstance(failure, dict) and isinstance(failure.get("failure_signature"), str):
                stage = {**stage, "status": "FAIL", "failure_signature": failure["failure_signature"]}
            binding = {**binding, "stage": stage}
            self._stage_bindings[stage_build_id] = binding
        return binding

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
        # A stage occurrence may already have frozen a pre-v2 signature. Re-observing it
        # must preserve that identity (including a partial checkpoint across stages), not
        # overwrite an immutable key or silently attach its history to a newly split cause.
        candidate = _failure_signature(binding.get("pipeline_id"), binding.get("module"), failed_stages, jobs)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        try:
            signature = _freeze_stage_failure_signature(
                self.state, self.run_id, [stage["stage_build_id"] for stage in failed_stages], candidate
            )
        except ValueError:
            return _failure("FAILURE_SIGNATURE_CONFLICT", status="INVALID")
        for stage in failed_stages:
            stage["failure_signature"] = signature
            self._stage_bindings[stage["stage_build_id"]]["stage"] = stage
        classification = _classification(failed_stages, jobs, binding["stage_classes"])
        refs = _build_evidence(build_id, binding, stages)
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


def trigger_binding(context: dict[str, Any], run_id: str) -> dict[str, Any]:
    """What a G7 trigger approval authorizes, derived from a context alone.

    Pure, so the approval card can show the exact object without a pipeline transport.
    """
    return {
        "operation": "ipipe.trigger",
        "run_id": run_id,
        "pipeline_id": context["pipeline_id"],
        "module": context["module"],
        "revision_set_id": context["revision_set_id"],
        "repositories": context["repositories"],
        "environment_fingerprint": context["environment_fingerprint"],
        "parameters": context["parameters"],
    }


def _context(profile: Any, revisions: Any, run_id: str, module: Any = None) -> dict[str, Any]:
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
    # A requirement that spans repositories has one pipeline per module, so the caller
    # names the repository this context is about. The first business repository stays
    # the default, which is the only answer for a single-repository profile. The test
    # repository is addressable too: its pipeline is registered like any other, and
    # leaving it out of the lookup made its own build impossible to bind.
    target = next(
        (item for item in business + [test_repo] if item.get("module") == module),
        None if module is not None else business[0],
    )
    if not isinstance(target, dict):
        return _failure("PIPELINE_IDENTITY_MISMATCH")
    pipeline_id = str(_registered_pipeline(pipeline, target.get("module")) or "")
    # A registered pipeline carries its own parameter and stage vocabulary, since two
    # pipelines over the same requirement rarely have the same stages.
    registered = next(
        (
            entry for entry in pipeline.get("pipelines") or []
            if isinstance(entry, dict) and entry.get("module") == target.get("module")
        ),
        {},
    )
    allowed = registered.get("allowed_parameters", pipeline.get("allowed_parameters"))
    parameters = revisions.get("parameters", {})
    stage_classes = registered.get("stage_classes", pipeline.get("stage_classes"))
    release_rule = registered.get("release_rule", pipeline.get("release_rule"))
    if not pipeline_id or not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed) or not isinstance(parameters, dict) or not isinstance(stage_classes, list) or not _nonempty(release_rule):
        return _failure("PROJECT_NOT_READY")
    expected = [("business", item.get("module"), item.get("branch")) for item in business]
    expected.append(("test", test_repo.get("module"), test_repo.get("branch")))
    actual = [(item.get("kind"), item.get("module"), item.get("branch")) for item in repositories]
    if expected != actual or any(not _nonempty(item.get("revision")) for item in repositories):
        return _failure("REVISION_SET_MISMATCH")
    revision_map = {item["module"]: item["revision"] for item in repositories}
    return {
        "ok": True,
        "reason_code": "OK",
        "pipeline_id": pipeline_id,
        "module": str(target.get("module") or ""),
        "target_branch": str(target.get("branch") or ""),
        "revision_set_id": revisions["revision_set_id"],
        "repositories": repositories,
        "revision_map": revision_map,
        "allowed_parameters": allowed,
        "parameters": parameters,
        "stage_classes": stage_classes,
        "release_rule": release_rule,
        "environment_fingerprint": _canonical_hash(environment),
    }


def _pipeline_modules(pipeline: Any) -> set[str]:
    """The repositories a pipeline configuration is bound to.

    A module pipeline names its repositories under `sources[].path`; the flat
    `module`/`space` fields only appear on build records, so both are accepted.
    """
    if not isinstance(pipeline, dict):
        return set()
    modules = {
        str(pipeline.get(key)) for key in ("module", "space")
        if _nonempty(pipeline.get(key))
    }
    for source in pipeline.get("sources") or []:
        if isinstance(source, dict) and _nonempty(source.get("path")):
            modules.add(str(source["path"]))
    return modules


def _matches_build(build: Any, context: dict[str, Any]) -> bool:
    if not isinstance(build, dict):
        return False
    pipeline_id = str(build.get("pipelineConfId") or build.get("pipeline_id") or "")
    module = str(build.get("module") or build.get("space") or "")
    if pipeline_id != context["pipeline_id"] or module != context["module"]:
        return False
    # A build record reports the revision it was triggered on, and only sometimes a map
    # over every repository or the parameters it ran with. Absent fields are not
    # evidence of a mismatch, so each is compared only when the record states it.
    revisions = build.get("revisions")
    if isinstance(revisions, dict) and revisions != context["revision_map"]:
        return False
    primary_revision = build.get("revision") or (build.get("trigger") or {}).get("revision")
    if primary_revision is not None and primary_revision != context["revision_map"].get(module):
        return False
    if primary_revision is None and not isinstance(revisions, dict):
        return False
    parameters = build.get("params", build.get("parameters"))
    return parameters is None or parameters == context["parameters"]


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


def _stage_id_from_jobs(value: dict[str, Any]) -> str:
    """The stage build id as reported by the stage's own jobs.

    The stage listing identifies a stage by its *configuration* id and only names the
    stage build on each job underneath it, so that is where the identity comes from.
    """
    for job in value.get("jobBuildBeans") or []:
        if isinstance(job, dict) and str(job.get("stageBuildId") or "").strip():
            return str(job["stageBuildId"])
    return ""


# Read-only confirmations of a manual-stage execute whose response names no stage.
_RERUN_CONFIRM_POLLS = 4
_RERUN_CONFIRM_INTERVAL = 5


def _rerun_response_stage(value: Any) -> str:
    """Accept explicit stage identities in API envelopes; boolean acks have no identity."""
    if not isinstance(value, dict):
        return ""
    for key in ("stageBuildId", "id"):
        identity = value.get(key)
        if isinstance(identity, (str, int)) and not isinstance(identity, bool) and str(identity):
            return str(identity)
    for key in ("entities", "data", "stageBuild"):
        identity = _rerun_response_stage(value.get(key))
        if identity:
            return identity
    return ""


def _epoch_seconds(value: Any) -> float | None:
    """A platform start time (epoch s/ms) or an ISO timestamp, as epoch seconds."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            seconds = float(value)
            return seconds / 1000 if seconds > 100_000_000_000 else seconds
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _started_after(stage: dict[str, Any], moment: Any) -> bool:
    """Whether the stage's start time is strictly after `moment`, with no skew allowance.

    Used against another attempt's *receipt* time: that receipt was written after its
    own run had started, so only a strictly later start is a different execution.
    """
    started = _epoch_seconds(stage.get("start_time"))
    boundary = _epoch_seconds(moment) if moment else None
    return started is not None and boundary is not None and started > boundary


def _rerun_started(stage: dict[str, Any], *, since: str | None = None) -> bool:
    """Whether `stage` shows an execution that began after the intent was claimed.

    A status alone proves nothing: a stage that was already SUCCESS or RUNNING before
    the request (an earlier run, a click on the iPipe page) looks exactly like one this
    request started. So the stage must carry a start time strictly later than the
    intent's creation, less _RERUN_START_CLOCK_SKEW_SECONDS for the platform/local clock
    difference. Without that evidence the outcome stays unknown (QUERY_REQUIRED /
    RERUN_NOT_STARTED_UNPROVEN); nothing is re-posted on the strength of a status.
    """
    # WAITTING/PENDING/SKIPPED alone do not prove an execution was accepted.
    if _status(stage) not in _SUCCESS | {"RUNNING", "SKIPPING", "BUILDING", "EXECUTING"} | _FAILURE:
        return False
    started = _epoch_seconds(stage.get("start_time"))
    claimed = _epoch_seconds(since) if since else None
    if started is None or claimed is None:
        return False
    return started > claimed - _RERUN_START_CLOCK_SKEW_SECONDS


def _normalize_stage(value: dict[str, Any]) -> dict[str, Any]:
    stage_id = str(value.get("id") or value.get("stageBuildId") or _stage_id_from_jobs(value) or "")
    return {
        "stage_build_id": stage_id,
        "name": str(value.get("stageName") or value.get("name") or stage_id),
        "status": _status(value),
        "start_time": value.get("startTime"),
        "class": str(value.get("class") or value.get("stageClass") or ""),
        "job_statuses": [
            _status(job) for job in value.get("jobBuildBeans") or [] if isinstance(job, dict)
        ],
        "stage_conf_id": str(value.get("stageConfId") or ""),
        "jobs": [
            {
                "job_build_id": str(job.get("id") or job.get("jobBuildId") or ""),
                "name": str(job.get("jobName") or job.get("name") or ""),
                "status": _status(job),
                "log_url": _job_log_url(job),
            }
            for job in value.get("jobBuildBeans") or [] if isinstance(job, dict)
        ],
    }


_EVIDENCE_STATUS: dict[str, str] = {
    **{token: "SUCCESS" for token in _SUCCESS},
    **{token: "FAILURE" for token in _FAILURE},
    **{token: "BLOCKED" for token in _MANUAL},
    **{token: "SUCCESS" for token in _SKIPPED},
}


def _evidence_status(raw: Any) -> str:
    """Map an iPipe stage/job status token to the 3-value schema enum (default FAILURE)."""
    return _EVIDENCE_STATUS.get(str(raw or "").upper(), "FAILURE")


def _terminal_evidence_status(status: Any) -> str:
    if status == "SUCCESS":
        return "SUCCESS"
    if status in ("MANUAL_WAIT", "RELEASE_WAITING"):
        return "BLOCKED"
    return "FAILURE"


def evidence_outcome(monitor_result: dict[str, Any]) -> dict[str, Any]:
    """The ipipe-evidence OUTCOME fields, mapped from a terminal monitor result.

    Owns only the iPipe-vocabulary -> schema mapping. The binding half (pipeline_id /
    module / revisions / release_rule / environment_fingerprint) is joined by the caller
    from the pinned submission, so it cannot drift from what G7 approved. Per the agreed
    design: statuses collapse to {SUCCESS, FAILURE, BLOCKED}; jobs[] is aggregated from
    stages[].jobs[] with a synthesized per-job evidence ref; a stage the API did not break
    into jobs contributes one implicit job so the schema's job_ids/jobs minItems hold.
    """
    stages_out: list[dict[str, Any]] = []
    jobs_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stage in monitor_result.get("stages") or []:
        stage_id = str(stage.get("stage_build_id") or "")
        stage_jobs = stage.get("jobs") or [{"job_build_id": f"{stage_id}-job", "status": stage.get("status")}]
        job_ids: list[str] = []
        for job in stage_jobs:
            job_id = str(job.get("job_build_id") or f"{stage_id}-job")
            job_ids.append(job_id)
            if job_id not in seen:
                seen.add(job_id)
                jobs_out.append({
                    "job_id": job_id,
                    "status": _evidence_status(job.get("status")),
                    "evidence_refs": [f"ipipe:job/{job_id}"],
                })
        stages_out.append({
            "stage_id": stage_id,
            "status": _evidence_status(stage.get("status")),
            "job_ids": job_ids,
        })
    evidence_refs = list(monitor_result.get("evidence_refs") or [])
    return {
        "status": _terminal_evidence_status(monitor_result.get("status")),
        "classification": monitor_result.get("classification") or "SUCCESS",
        "failure_signature": monitor_result.get("failure_signature"),
        "stages": stages_out,
        "jobs": jobs_out,
        "remote_evidence_refs": evidence_refs,
        "release_evidence": evidence_refs,
        "build_id": monitor_result.get("build_id"),
    }


def _job_log_url(job: dict[str, Any]) -> str:
    for entry in job.get("logs") or []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"].startswith("http"):
            return entry["url"]
    return ""


def _default_log_reader(url: str) -> dict[str, Any]:
    from ipipe_logs import fetch, parse

    fetched = fetch(url)
    if not fetched.get("ok"):
        return fetched
    return {**parse(fetched["text"]), "url": url}


def _stage_passed(stage: dict[str, Any]) -> bool:
    """Whether a stage counts as passing evidence.

    A stage is reported SKIPPED as soon as any job under it is skipped, even when the
    rest ran and succeeded: BGW skips its 100G jobs and runs the 25G ones in the same
    unit-test stage. That is passing evidence, so a skipped stage is accepted when a job
    under it succeeded and none failed. A stage with no job that ran is not evidence.
    """
    status = stage.get("status")
    if status in _SUCCESS:
        return True
    if status not in _SKIPPED:
        return False
    jobs = stage.get("job_statuses") or []
    return any(job in _SUCCESS for job in jobs) and not any(job in _FAILURE for job in jobs)


def _build_passed(aggregate: str, stages: list[dict[str, Any]]) -> bool:
    """Whether a finished build is passing evidence.

    iPipe reports a build SKIPPED when any stage was skipped, so a build whose only
    skipped stages were excluded by the pipeline's skip expression still passes —
    provided every other stage passed and at least one stage actually ran.
    """
    if aggregate not in _SUCCESS | _SKIPPED or not stages:
        return False
    ran = [stage for stage in stages if not stage.get("skipped_by_expression")]
    if not ran or not all(_stage_passed(stage) for stage in ran):
        return False
    return aggregate in _SUCCESS or len(ran) < len(stages) or any(
        stage.get("status") in _SKIPPED for stage in ran)


def _real_jobs(detail: Any) -> list[dict[str, Any]]:
    """The job entries of a stage detail response (`entities.realJobBuilds`, grouped)."""
    entities = detail.get("entities") if isinstance(detail, dict) else None
    groups = (entities or {}).get("realJobBuilds") if isinstance(entities, dict) else None
    jobs: list[dict[str, Any]] = []
    for group in groups or []:
        for job in group if isinstance(group, list) else [group]:
            if isinstance(job, dict):
                jobs.append(job)
    return jobs


def _job_skip_reasons(detail: Any) -> list[str]:
    """Every job's `skipByStage` in a stage detail response (None when absent)."""
    reasons: list[str] = []
    pending: list[Any] = [detail]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "skipByStage" in value and (value.get("id") or value.get("jobBuildId")):
                reasons.append(str(value.get("skipByStage") or ""))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return reasons


def _normalize_job(value: dict[str, Any]) -> dict[str, Any]:
    job_id = str(value.get("id") or value.get("jobBuildId") or "")
    normalized = {
        "job_build_id": job_id,
        "name": str(value.get("jobName") or value.get("name") or job_id),
        "status": _status(value),
        "message": _redact_text(str(value.get("message") or value.get("statusMessage") or ""))[:1024],
    }
    # Structured platform fields only (enumerations, never free text), carried so the
    # environment classification can rest on them; see `_structured_environment_class`.
    for field, keys in _STRUCTURED_ENVIRONMENT_FIELDS.items():
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                normalized[field] = raw.strip().upper()
                break
    return normalized


def _embedded_stages(build: dict[str, Any]) -> list[dict[str, Any]]:
    value = build.get("stageBuilds", build.get("stages", []))
    return value if isinstance(value, list) else []


def _status(value: dict[str, Any]) -> str:
    return str(value.get("status") or value.get("releaseStatus") or "UNKNOWN").upper()


# Structured job fields the platform may return, mapped to their normalized name. The
# values are enumerations; nothing here reads a message or a name.
_STRUCTURED_ENVIRONMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "error_type": ("errorType", "failType", "failureType", "systemErrorType", "errorCategory"),
    "agent_status": ("agentStatus", "agentState", "runnerStatus"),
}
# Structured values that are environment facts (an authoritative ENV_* class).
_ENVIRONMENT_ERROR_TYPES: dict[str, str] = {
    "AGENT_OFFLINE": "ENV_UNSATISFIED",
    "AGENT_LOST": "ENV_UNSATISFIED",
    "NO_AVAILABLE_AGENT": "ENV_UNSATISFIED",
    "RUNNER_OFFLINE": "ENV_UNSATISFIED",
    "RESOURCE_INSUFFICIENT": "ENV_UNSATISFIED",
    "RESOURCE_NOT_ENOUGH": "ENV_UNSATISFIED",
    "QUEUE_TIMEOUT": "ENV_TRANSIENT",
    "SCHEDULE_TIMEOUT": "ENV_TRANSIENT",
}
_OFFLINE_AGENT_STATES = frozenset({"OFFLINE", "LOST", "DISCONNECTED"})
# Advisory, never authoritative: a keyword hit in names or messages. "timeout" and
# "network" are as likely a test case (`test_syn_proxy_timeout`) or a source file
# (`network.c`) as an environment, so the hint is only evidence for the diagnosis.
HEURISTIC_ENVIRONMENT_CLASS = "ENVIRONMENT_SUSPECTED"


def _structured_environment_class(jobs: list[dict[str, Any]]) -> str | None:
    """An ENV_* class proven by structured platform fields, or None.

    Only enumerated fields count (`error_type`, `agent_status` from the raw job). The
    field names follow the platform's structured error shape; when the gateway returns
    none of them -- the case for every response observed so far -- nothing is claimed
    and the failure goes through ordinary diagnosis and G6.
    """
    for job in jobs:
        if not isinstance(job, dict):
            continue
        mapped = _ENVIRONMENT_ERROR_TYPES.get(str(job.get("error_type") or ""))
        if mapped:
            return mapped
        if str(job.get("agent_status") or "") in _OFFLINE_AGENT_STATES:
            return "ENV_UNSATISFIED"
    return None


def _classification(stages: list[dict[str, Any]], jobs: list[dict[str, Any]], stage_classes: list[str]) -> str:
    structured = _structured_environment_class(jobs)
    if structured is not None:
        return structured
    text = " ".join(
        [str(stage.get("name") or "") + " " + str(stage.get("class") or "") for stage in stages]
        + [str(job.get("message") or "") + " " + str(job.get("name") or "") for job in jobs if isinstance(job, dict)]
    ).lower()
    if any(word in text for word in ("unit", "test", "regression", "integration", "assert")):
        return "TEST_FAILURE"
    if any(word in text for word in ("compile", "build", "link", "code", "interface")):
        return "CODE_FAILURE"
    if any(word in text for word in ("environment", "capacity", "runner", "network", "timeout", "infra")):
        return HEURISTIC_ENVIRONMENT_CLASS
    return "MIXED_FAILURE" if len(stages) > 1 else "PIPELINE_FAILURE"


def _same_stage(requested: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Whether two stage builds are two runs of the same stage.

    A re-executed stage keeps its stage configuration and its name; only the stage build
    id is new. The configuration id decides when both sides carry one, and the name is the
    fallback for a binding written before it was recorded.
    """
    left, right = str(requested.get("stage_conf_id") or ""), str(candidate.get("stage_conf_id") or "")
    if left and right:
        return left == right
    return bool(requested.get("name")) and requested.get("name") == candidate.get("name")


_FAILURE_SIGNATURE_PREFIX = "ipipe-failure:v2:"
_OCCURRENCE_FIELD = re.compile(
    r"\b(?P<label>(?:build|request|req)(?:[-_ ]?(?:id|number|no))?"
    r"|rev(?:ision)?|commit(?:[-_ ]?(?:id|sha))?|hash|sha(?:256)?)"
    r"(?P<separator>[\"']?\s*[:=#]\s*[\"']?|\s+[\"']?)"
    r"(?P<value>[a-z0-9][a-z0-9_.-]*)(?![a-z0-9_])", re.IGNORECASE,
)
_CAUSE_FIELD = re.compile(
    r"\b(?:(?:test[-_ ]?)?case(?:[-_ ]?id)?|test(?:[-_ ]?id)?"
    r"|error[-_ ]?code|status|errno|hresult)"
    r"(?:[\"']?\s*[:=#]\s*[\"']?|\s+[\"']?)(?P<value>[a-z0-9][a-z0-9_.-]*)", re.IGNORECASE,
)
_BUILD_PATH = re.compile(r"(?<![\w])(?:[a-z]:[\\/]|/)[^\s'\"]+", re.IGNORECASE)
# Volatile CI checkout/build roots whose leading directory is machine-specific.
# The relative source path below the root is preserved (so api/test_base.py and
# dns/test_base.py stay distinct); only the volatile root, and a numeric/uuid
# checkout directory right under it, are normalized. A too-narrow list makes the
# same root cause fragment across runs when a site's CI root is missing here, so
# this covers the common Linux CI roots — extend it for a site-specific root
# (baidu BGW checkouts live under /home and /ssd*).
_BUILD_ROOT = re.compile(
    r"^(?:[a-z]:)?/(?:work|workspace|build|builds|tmp|var/tmp|var/lib|"
    r"home|homes|users|root|opt|data|srv|mnt|media|export|"
    r"ssd\w*|nvme\w*|disk\d*|jenkins|ci|runner|agent)/",
    re.IGNORECASE,
)


def _freeze_stage_failure_signature(
    state: StateStore, run_id: str, stage_ids: list[str], candidate: str
) -> str:
    """Choose and checkpoint one signature atomically across an occurrence's stages.

    A pre-v2 partial checkpoint supplies the old identity. Conflicting preexisting rows
    are evidence corruption, never a reason to pick one. One transaction also prevents
    concurrent monitor calls from writing different identities to different stage keys.
    This uses StateStore's connection/encoding policy without changing its schema.
    """
    from state_store import _encode

    keys = sorted({f"ipipe.stage-failure:{run_id}:{stage_id}" for stage_id in stage_ids})
    if not keys:
        return candidate
    with state._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        frozen: set[str] = set()
        missing = []
        for key in keys:
            row = connection.execute(
                "SELECT result_json FROM idempotency_results WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:
                missing.append(key)
                continue
            recorded = json.loads(row["result_json"])
            value = recorded.get("failure_signature") if isinstance(recorded, dict) else None
            if not isinstance(value, str) or not value:
                raise ValueError("FAILURE_SIGNATURE_CONFLICT")
            frozen.add(value)
        if len(frozen) > 1:
            raise ValueError("FAILURE_SIGNATURE_CONFLICT")
        signature = next(iter(frozen)) if frozen else candidate
        encoded = _encode({"failure_signature": signature})
        now = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            "INSERT INTO idempotency_results(idempotency_key, result_json, created_at) VALUES (?, ?, ?)",
            [(key, encoded, now) for key in missing],
        )
        return signature


def _normalize_error(text: Any) -> str:
    """Normalize occurrence noise while retaining cause identity (signature v2).

    Numeric/hex length alone says nothing about volatility: error codes, test IDs and
    exception names remain literal and case-sensitive. Only explicit occurrence fields,
    UUIDs, timestamps and build-root paths are normalized. Build paths retain the filename
    and pytest ``::case`` suffix. No 200-character cut may hide the actual failed case.
    The runtime already bounds raw job messages before passing them here.
    """
    if not isinstance(text, str) or not text:
        return ""
    protected: list[str] = []
    marker = "\x00cause-"
    while marker in text:
        marker += "-"

    def protect_cause(match: re.Match[str]) -> str:
        # Only UUID/date-looking values need protection from the global unit rules.
        # Masking prose ("test build 987") would hide the real build label from the pass
        # below and reintroduce occurrence noise.
        if "-" not in match["value"]:
            return match[0]
        protected.append(match["value"])
        prefix = match[0][:-len(match["value"])]
        return f"{prefix}{marker}{len(protected) - 1}\x00"

    def occurrence(match: re.Match[str]) -> str:
        value = match["value"].rstrip(".")
        # The label supplies the semantics; the token check avoids consuming prose such
        # as "build failed" and "request rejected" as if those words were identifiers.
        if value.lower().endswith(("error", "exception")):
            return match[0]
        if not (any(char.isdigit() for char in value) or re.fullmatch(r"[a-f0-9]{6,}", value, re.IGNORECASE)):
            return match[0]
        suffix = match["value"][len(value):]
        return f"{match['label'].lower()}{match['separator']}<id>{suffix}"

    def build_path(match: re.Match[str]) -> str:
        path = match[0].replace("\\", "/")
        root = _BUILD_ROOT.match(path)
        if root is None:
            return match[0]
        stripped = path.rstrip(").,;]}")
        punctuation = path[len(stripped):]
        source, separator, case = stripped.partition("::")
        source = re.sub(r":\d+(?::\d+)?$", "", source)
        relative = source[root.end():].split("/")
        # The generated checkout/build directory is volatile; retain the source's
        # relative path so api/test_base.py and dns/test_base.py remain different
        # tests. A volatile directory is not always the first segment under the
        # root (e.g. /opt/ci/run-111/... or /home/work/ci-1/...), so mask every
        # directory segment that carries a number, uuid or timestamp while keeping
        # the filename and any non-volatile source directories.
        for index in range(len(relative) - 1):
            segment = relative[index]
            if segment in {"<uuid>", "<ts>"} or any(char.isdigit() for char in segment):
                relative[index] = "<build-id>"
        return f"<build-path>/{'/'.join(relative)}{separator}{case}{punctuation}"

    normalized = _CAUSE_FIELD.sub(protect_cause, text)
    normalized = _OCCURRENCE_FIELD.sub(occurrence, normalized)
    normalized = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<uuid>", normalized, flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?\b",
        "<ts>", normalized, flags=re.IGNORECASE,
    )
    normalized = _BUILD_PATH.sub(build_path, normalized)
    for index, value in enumerate(protected):
        normalized = normalized.replace(f"{marker}{index}\x00", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _failure_signature(
    pipeline_id: Any, module: Any, stages: list[dict[str, Any]], jobs: list[dict[str, Any]]
) -> str:
    """A STABLE root-cause signature, independent of this occurrence's build identity.

    New occurrences carry an explicit v2 namespace. Historical unversioned digests may
    have merged unrelated causes and are never automatic aliases; frozen occurrences
    retain their original identity in _failure_evidence. See references/failure-signatures.md.

    The same structural failure recurring in a later run is the same root cause, so the
    signature is hashed over what identifies the failure across runs — the pipeline and
    module, each failed stage/job by its configuration identity and status, and a normalized
    fingerprint of each failed job's error message (R-M4) — never over build_id /
    stage_build_id / job_build_id, which are fresh every build and would split one root cause
    into a new FailureCase every run. The error fingerprint is what separates two distinct
    failures at the SAME stage/job (a different assertion, a different exception) so they are
    not merged into one cross-run case; `_normalize_error` strips the volatile tokens so the
    same error still matches across builds. A job is included only when it carries a real name
    (its normalized name falls back to the occurrence job id when absent, which would
    reintroduce per-run noise), and stages/jobs are sorted so ordering never shifts the hash.
    """
    value = {
        "pipeline_id": str(pipeline_id or ""),
        "module": str(module or ""),
        "stages": sorted(
            {
                (
                    str(item.get("stage_conf_id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("status") or ""),
                )
                for item in stages if isinstance(item, dict)
            }
        ),
        "jobs": sorted(
            {
                (
                    str(item.get("name") or ""),
                    str(item.get("status") or ""),
                    _normalize_error(item.get("message")),
                )
                for item in jobs
                if isinstance(item, dict)
                and item.get("name")
                and item.get("name") != item.get("job_build_id")
            }
        ),
    }
    return _FAILURE_SIGNATURE_PREFIX + _canonical_hash(value)


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


def _build_evidence(
    build_id: str, context: dict[str, Any], stages: list[dict[str, Any]] | None = None
) -> list[str]:
    """Expose the complete module/build/stage binding in every remote receipt."""
    module = str(context.get("module") or "")
    refs = [f"ipipe:build/{build_id}"]
    if module:
        module_token = _evidence_token(module)
        refs.append(f"ipipe:module-build/{module_token}-{build_id}")
    for repo_module, revision in sorted((context.get("revision_map") or {}).items()):
        refs.append(f"revision-{revision}")
        refs.append(f"ipipe:module-revision/{_evidence_token(repo_module)}-{revision}")
    for stage in stages or []:
        stage_id = stage.get("stage_build_id") if isinstance(stage, dict) else None
        if stage_id:
            refs.append(f"ipipe:stage/{stage_id}")
            refs.append(f"ipipe:module-stage/{_evidence_token(module)}-{build_id}-{stage_id}")
    return list(dict.fromkeys(refs))


def _registered_release_rule(pipeline: Any, module: Any) -> Any:
    entries = pipeline.get("pipelines") if isinstance(pipeline, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("module") == module:
            return entry.get("release_rule", pipeline.get("release_rule"))
    return pipeline.get("release_rule") if isinstance(pipeline, dict) else None


def _evidence_token(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))


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
    if gate_of(record.get("action")) != action:
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


def _started_before(build: Any, since: Any, skew_seconds: float) -> bool:
    """True when the build record states a start time earlier than `since` minus skew.

    Records without a usable start time are not excluded: absence is not evidence.
    """
    if not isinstance(build, dict):
        return False
    raw = build.get("startTime", build.get("buildTime"))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    try:
        floor = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)
    started = datetime.fromtimestamp(raw / 1000 if raw > 10**11 else raw, tz=timezone.utc)
    return (floor - started).total_seconds() > skew_seconds


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


def _failed_before_dispatch(error: BaseException) -> bool:
    """True when a programming error was raised by the call itself, not inside the callee.

    A traceback with no frame below the caller means the interpreter refused the call
    (missing attribute, wrong arity) before the client method ran, so no request can
    have been sent. An error raised deeper may come after the POST and stays unknown.
    """
    return (
        isinstance(error, (AttributeError, TypeError, NameError))
        and not isinstance(error, IpipeTransportError)
        and error.__traceback__ is not None
        and error.__traceback__.tb_next is None
    )


# How long a claimed rerun may stay unchanged before "not started" is reported.
_RERUN_OBSERVATION_GRACE_SECONDS = 300
# Platform clock vs. control-plane clock when deciding a stage started *after* an
# intent was claimed. Small on purpose: the POST follows the claim within seconds, and
# any stage state older than this window is from a previous execution.
_RERUN_START_CLOCK_SKEW_SECONDS = 30
# Re-issues of one approval after an attempt abandoned with proof of a local error.
_LOCAL_ERROR_RETRY_LIMIT = 1
_URL_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_TOKENISH = re.compile(r"(?i)\bBearer[- ][A-Za-z0-9._~+/=-]+|[A-Za-z0-9_+/=-]{32,}")


def _body_shape(excerpt: str, *, truncated: bool) -> dict[str, Any]:
    """Field names and a short redacted text fragment of an error body, nothing else."""
    shape: dict[str, Any] = {}
    if not truncated:
        try:
            parsed = json.loads(excerpt)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            shape["body_type"] = "object"
            shape["body_keys"] = sorted(str(key)[:40] for key in parsed)[:20]
            code = parsed.get("code")
            if isinstance(code, (int, str)) and not isinstance(code, bool) and len(str(code)) <= 20:
                shape["business_code"] = code
        elif isinstance(parsed, list):
            shape["body_type"] = "array"
    from persistence_policy import redact_text
    fragment = _TOKENISH.sub("[REDACTED]", _URL_TEXT.sub("[URL]", redact_text(excerpt)))
    shape["body_fragment"] = fragment[:200]
    return shape


def _transport_reason(error: Exception, fallback: str) -> str:
    return error.reason_code if isinstance(error, IpipeTransportError) else fallback
