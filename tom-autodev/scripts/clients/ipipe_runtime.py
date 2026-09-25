from __future__ import annotations

from execution_guard import guard_execution

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from approval_ledger import ApprovalLedger, gate_of
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
        return {
            "operation": "ipipe.trigger",
            "run_id": self.run_id,
            "pipeline_id": context["pipeline_id"],
            "module": context["module"],
            "revision_set_id": context["revision_set_id"],
            "repositories": context["repositories"],
            "environment_fingerprint": context["environment_fingerprint"],
            "parameters": context["parameters"],
        }

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
            return dict(completed["receipt"]["response"])
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        claim = self.state.claim_intent(self.run_id, "ipipe.trigger", key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("TRIGGER_CONFLICT")
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
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
            if aggregate in _SUCCESS and all(_stage_passed(stage) for stage in normalized_stages):
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
        """The build product a downstream stage has to download, for one module.

        A manual stage that asks a person to paste a download command is asking for a fact
        the pipeline already published: the compile job carries `productHttpUrl`. Finding
        it costs three reads -- the build for the revision, its stages, the compile
        stage's jobs -- and removes a hand-copied parameter.
        """
        try:
            builds = self.api.builds_by_revision(module, revision, pipeline_id)
        except Exception as error:
            return _failure(_transport_reason(error, "PIPELINE_TRANSIENT"))
        for build in builds:
            build_id = _build_id(build)
            if not build_id:
                continue
            try:
                stages = self.api.pipeline_stage_info(build_id)
            except Exception as error:
                return _failure(_transport_reason(error, "PIPELINE_TRANSIENT"))
            for stage in stages:
                stage_id = str(stage.get("id") or stage.get("stageBuildId") or "")
                if not stage_id or _status(stage) not in _SUCCESS:
                    continue
                try:
                    detail = self.api.stage_detail(stage_id)
                except Exception as error:
                    return _failure(_transport_reason(error, "PIPELINE_TRANSIENT"))
                for group in (detail.get("entities") or {}).get("realJobBuilds") or []:
                    for job in group if isinstance(group, list) else []:
                        if not isinstance(job, dict) or _status(job) not in _SUCCESS:
                            continue
                        url = ((job.get("realJobBuild") or {}).get("productHttpUrl") or "")
                        if isinstance(url, str) and url.startswith("http"):
                            return {
                                "ok": True, "reason_code": "OK", "module": module,
                                "revision": revision, "build_id": build_id,
                                "stage_build_id": stage_id,
                                "job_build_id": str(job.get("id") or ""),
                                "product_url": url,
                            }
        return _failure("PRODUCT_URL_NOT_PUBLISHED", module=module, revision=revision)

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

        key = f"ipipe.rerun:{self.run_id}:{stage_build_id}"
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
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        claim = self.state.claim_intent(self.run_id, "ipipe.rerun", key, payload)
        if claim["status"] != "CLAIMED":
            return _failure("RERUN_CONFLICT" if claim["status"] == "CONFLICT" else "QUERY_REQUIRED", retry_allowed=False)
        intent = claim["intent"]
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        try:
            response = self.api.manual_execute_stage(stage_build_id, dict(parameters or {}))
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
        if not response_stage:
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
        if started is None or _status(started) in _FAILURE | _MANUAL:
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
        if current_stage is not None and _status(current_stage) not in _FAILURE | _MANUAL:
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
        successor = next(
            (
                item for item in normalized
                if item["stage_build_id"] != payload["stage_build_id"]
                and _same_stage(requested, item)
                and _status(item) not in _FAILURE | _MANUAL
            ),
            None,
        )
        if successor is None:
            return None
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        binding = self._load_build_binding(payload["build_id"])
        if binding is not None:
            self._bind_stage(payload["build_id"], binding, successor)
        return self._save_rerun_receipt(
            intent["intent_id"], payload, started_stage_build_id=successor["stage_build_id"]
        )

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
        if _status(current) not in _SUCCESS:
            return _failure("PIPELINE_NOT_SUCCESSFUL")
        try:
            stages = [_normalize_stage(stage) for stage in self._build_stages(build_id, current)]
        except Exception as error:
            return _failure(_transport_reason(error, "STAGE_QUERY_FAILED"))
        stage_identity_failure = _stage_identity_failure(stages)
        if stage_identity_failure is not None:
            return _failure(stage_identity_failure)
        if any(not _stage_passed(stage) for stage in stages):
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


def _normalize_stage(value: dict[str, Any]) -> dict[str, Any]:
    stage_id = str(value.get("id") or value.get("stageBuildId") or _stage_id_from_jobs(value) or "")
    return {
        "stage_build_id": stage_id,
        "name": str(value.get("stageName") or value.get("name") or stage_id),
        "status": _status(value),
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
