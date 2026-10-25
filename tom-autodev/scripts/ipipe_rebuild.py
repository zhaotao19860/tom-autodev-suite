"""Controlled re-build of a run's pipeline when the failed build's product is gone.

Why this exists: every stage re-run (`ipipe-rerun`) sets up its environment from the
compile product of the build it belongs to. When that product can no longer be
downloaded (the platform expired it: HTTP 403/404), no stage of the build can ever pass
again and re-running one only burns a G8. The remedy is a new build of the *same*
revisions, which is a second pipeline trigger and therefore needs its own approval.

The flow, each step a separate CLI invocation of `ipipe-rebuild`:

1. plan (default, read-only): checks that the run is where a re-build is justified,
   proves with a read-only download probe that the product is really gone, proves the
   CR patchsets still equal the frozen revisions, and prints the G8 hash.
2. `--request-g8`: pins the exact material and delivers the G8. Executes nothing.
3. `--approval-id/--input-hash`: triggers once (or settles an unknown outcome with GET
   only), then rebinds the run to the new build.
4. `continue`: monitors the new build and prepares the manual-stage G8 as usual.

Nothing here edits an old event, receipt or artifact. Rebinding appends one audit event,
writes a superseded marker for the old build and marks the pending DIAGNOSE job
SUPERSEDED, all in one transaction.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from execution_guard import guard_execution
import build_markers
from pipeline_plan import frozen_plan

DEFAULT_EVENT_TYPE = "GERRIT_CHANGE"
# Platform answers that mean "this product is gone", as opposed to "could not tell".
GONE_STATUSES = frozenset({403, 404})
_MATERIAL_KEY = "ipipe.g8-material"
_REBIND_KEY = "ipipe-rebuild-rebind"


def _failure(reason_code: str, run_id: str | None = None, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "reason_code": reason_code, **details}
    if run_id is not None:
        result["run_id"] = run_id
    return result


def _not_justified(run_id: str, detail: str, **details: Any) -> dict[str, Any]:
    return _failure("REBUILD_NOT_JUSTIFIED", run_id, detail=detail, retry_allowed=False, **details)


def material_key(run_id: str, input_hash: str) -> str:
    return f"{_MATERIAL_KEY}:{run_id}:{input_hash}"


def _position(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """The event the run is at, and whether a re-build may start from it."""
    events = orchestrator.state.events(run_id)
    if not events:
        return _failure("RUN_NOT_FOUND", run_id)
    current = events[-1]
    state = current.get("state")
    payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
    if state == "DIAGNOSE":
        # Only a diagnosis of a failed pipeline qualifies; a Review or Plan diagnosis
        # has no build to replace.
        if payload.get("previous_state") != "IPIPE":
            return _not_justified(run_id, "DIAGNOSE_NOT_FROM_IPIPE", state=state)
    elif state != "IPIPE":
        return _not_justified(run_id, "STATE_NOT_ELIGIBLE", state=state)
    return {"ok": True, "events": events, "current": current, "state": state, "payload": payload}


def current_build(
    orchestrator: Any, run_id: str, plan: dict[str, Any], module: str
) -> dict[str, Any]:
    """The one build this run currently follows for `module`: owned, not superseded."""
    target = plan["modules"][module]
    found: dict[str, dict[str, Any]] = {}
    for key, stored in orchestrator.state.idempotency_results_with_prefix(
        f"ipipe.build-binding:{run_id}:"
    ):
        binding = stored.get("binding") if isinstance(stored, dict) else None
        build_id = stored.get("build_id") if isinstance(stored, dict) else None
        if (
            not isinstance(binding, dict) or not isinstance(build_id, str)
            or stored.get("run_id") != run_id
            or binding.get("module") != module
            or binding.get("pipeline_id") != target["pipeline_id"]
            or binding.get("revision_set_id") != plan["revision_set"]["revision_set_id"]
        ):
            continue
        if build_markers.is_excluded(orchestrator.state, run_id, build_id):
            continue
        found[build_id] = binding
    if not found:
        return _failure("BUILD_OWNERSHIP_UNVERIFIED", run_id, module=module,
                        detail="no owned build; run ipipe-adopt first")
    if len(found) > 1:
        return _failure("BUILD_AMBIGUOUS", run_id, module=module, candidates=sorted(found))
    build_id, binding = next(iter(found.items()))
    return {"ok": True, "build_id": build_id, "binding": binding}


def _archived_build_status(orchestrator: Any, run_id: str, build_id: str) -> str | None:
    """The status of the newest archived IPIPE evidence for `build_id`, if any."""
    status = None
    for artifact in orchestrator.artifacts.phase_artifacts(run_id, "IPIPE"):
        content = ((artifact or {}).get("envelope") or {}).get("content") or {}
        if artifact.get("valid") and str(content.get("build_id") or "") == build_id:
            status = content.get("status")
    return status


def _diagnosed_build_error(
    orchestrator: Any, position: dict[str, Any], build_id: str, run_id: str
) -> dict[str, Any] | None:
    """In DIAGNOSE, the diagnosed failure must be the failure of the build being replaced."""
    if position["state"] != "DIAGNOSE":
        return None
    artifact_id = position["payload"].get("artifact_id")
    artifact = orchestrator.artifacts.phase_artifact(artifact_id) if isinstance(artifact_id, str) else {}
    content = ((artifact or {}).get("envelope") or {}).get("content") or {}
    if not (artifact or {}).get("valid") or content.get("build_id") != build_id:
        return _not_justified(run_id, "DIAGNOSED_BUILD_MISMATCH", build_id=build_id)
    if content.get("status") != "FAILURE":
        return _not_justified(run_id, "DIAGNOSED_BUILD_NOT_FAILED", build_id=build_id)
    return None


def probe_product(api: Any, build_id: str, module: str) -> dict[str, Any]:
    """Read-only: is the build's compile product still downloadable?

    Reuses the platform adapter's own download check (a 2-byte ranged read that follows
    no redirect with the irepo token). Neither the product URL, the signed location nor
    any token is returned: only the status the platform answered with.
    """
    try:
        stages = api.pipeline_stage_info(build_id)
    except Exception as error:  # noqa: BLE001 - reported, never guessed
        return {"verdict": "INCONCLUSIVE", "reason_code": getattr(error, "reason_code", "STAGE_QUERY_FAILED")}
    for stage in stages:
        stage_id = str(stage.get("id") or stage.get("stageBuildId") or "")
        if not stage_id or str(stage.get("status") or "").upper() not in {"SUCC", "SUCCESS", "SUCCEEDED"}:
            continue
        try:
            detail = api.stage_detail(stage_id)
        except Exception as error:  # noqa: BLE001
            return {"verdict": "INCONCLUSIVE", "reason_code": getattr(error, "reason_code", "STAGE_DETAIL_FAILED")}
        entity = detail.get("entities", detail) if isinstance(detail, dict) else {}
        for group in entity.get("realJobBuilds", []) if isinstance(entity, dict) else []:
            for job in group:
                real = job.get("realJobBuild") or {}
                url = real.get("productHttpUrl")
                if job.get("jobType") != "AGILE_COMPILE" or not isinstance(url, str) or not url:
                    continue
                if not hasattr(api, "verify_product_download"):
                    return {"verdict": "INCONCLUSIVE", "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED"}
                checked = api.verify_product_download(url, module)
                if not isinstance(checked, dict):
                    return {"verdict": "INCONCLUSIVE", "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED"}
                base = {"stage_build_id": stage_id, "job_build_id": str(job.get("id") or "")}
                if checked.get("ok") is True:
                    return {"verdict": "AVAILABLE", **base}
                status = checked.get("http_status")
                if isinstance(status, int) and status in GONE_STATUSES:
                    return {"verdict": "UNAVAILABLE", "http_status": status, **base}
                return {"verdict": "INCONCLUSIVE", "reason_code": checked.get("reason_code"),
                        **({"http_status": status} if isinstance(status, int) else {}), **base}
    return {"verdict": "INCONCLUSIVE", "reason_code": "COMPILE_PRODUCT_NOT_FOUND"}


def revision_check(
    orchestrator: Any, run_id: str, profile: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Every CR the plan froze must still be at the frozen patchset (read-only).

    A changed patchset means the frozen revision set no longer describes what the CR
    would build, so the approval would authorize a build nobody reviewed. A module the
    plan marks changed that cannot be checked fails closed; a repository with no open CR
    to ask (a baseline pin, or a test commit whose CR is not open) is only reported.
    """
    import orchestrator as controller  # lazy: the CLI module imports this one

    frozen = {item["module"]: item["revision"] for item in plan["revision_set"]["repositories"]}
    numbers: dict[str, str] = {}
    for submission in plan.get("submissions", []):
        stored = orchestrator.artifacts.get(submission["artifact_id"])
        try:
            receipt = json.loads(stored["content"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            return _failure("SUBMISSION_CONTENT_INVALID", run_id)
        if isinstance(receipt, dict) and receipt.get("module") and receipt.get("change_number"):
            numbers[str(receipt["module"])] = str(receipt["change_number"])
    repositories = {
        item["module"]: item for item in [*profile.get("business_repos", []), profile["test_repo"]]
        if isinstance(item, dict) and item.get("module")
    }
    test_module = profile["test_repo"]["module"]
    checked: dict[str, Any] = {}
    unverified: list[str] = []
    for module, revision in frozen.items():
        if module in (plan.get("unchanged") or {}):
            continue
        number = numbers.get(module)
        if number is None and module == test_module:
            number = controller._open_test_repo_cr(profile, run_id)
        if number is None:
            if module in plan["modules"]:
                return _failure("REVISION_UNVERIFIED", run_id, module=module, detail="NO_CHANGE_NUMBER")
            unverified.append(module)
            continue
        current = controller._current_patchset(module, repositories[module].get("path"), number)
        if current is None:
            return _failure("REVISION_UNVERIFIED", run_id, module=module, change_number=number,
                            detail="CR_NOT_OPEN_OR_UNREADABLE")
        if current != revision:
            return _failure("REVISION_DRIFT", run_id, module=module, change_number=number,
                            frozen=revision, current=current, retry_allowed=False)
        checked[module] = {"change_number": number, "revision": revision}
    return {"ok": True, "checked": checked, "unverified": sorted(unverified)}


def plan(
    orchestrator: Any, runtime: Any, run_id: str, *, reason: Any, module: Any = None,
    event_type: Any = DEFAULT_EVENT_TYPE, attempt: Any = None, skip_probe: bool = False,
) -> dict[str, Any]:
    """Read-only preconditions plus the exact binding and hash a G8 would be requested for.

    `attempt` is only passed when re-deriving a binding that was already requested (the
    material pins it); a fresh plan always takes the next attempt.
    """
    position = _position(orchestrator, run_id)
    if not position.get("ok"):
        return position
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    profile = pinned["profile"]
    try:
        frozen = frozen_plan(position["events"])
    except ValueError as error:
        return _failure(str(error), run_id)
    if frozen is None:
        return _failure("FROZEN_PRODUCT_PLAN_REQUIRED", run_id)
    required = frozen["required_modules"]
    target = module if module is not None else (required[0] if len(required) == 1 else None)
    if target is None:
        return _failure("REBUILD_MODULE_REQUIRED", run_id, required_modules=required)
    if target not in frozen["modules"]:
        return _failure("PIPELINE_IDENTITY_MISMATCH", run_id, module=target)
    owned = current_build(orchestrator, run_id, frozen, target)
    if not owned.get("ok"):
        return owned
    original = owned["build_id"]
    mismatch = _diagnosed_build_error(orchestrator, position, original, run_id)
    if mismatch is not None:
        return mismatch
    probe = {"verdict": "SKIPPED"}
    if not skip_probe:
        probe = probe_product(runtime.api, original, target)
        if probe["verdict"] == "AVAILABLE":
            return _not_justified(run_id, "PRODUCT_STILL_DOWNLOADABLE", build_id=original, probe=probe)
        if probe["verdict"] != "UNAVAILABLE":
            return _not_justified(run_id, "PRODUCT_STATE_INCONCLUSIVE", build_id=original, probe=probe)
    if position["state"] == "IPIPE":
        # From IPIPE there is no diagnosis naming the failed build, so the replacement
        # needs its own proof: the build being replaced actually failed, or its compile
        # product was just proven gone. A build that succeeded and whose product was not
        # probed is the run's good evidence and is not replaced on request.
        archived = _archived_build_status(orchestrator, run_id, original)
        if archived != "FAILURE" and probe["verdict"] != "UNAVAILABLE":
            return _not_justified(
                run_id, "REPLACED_BUILD_NOT_FAILED", build_id=original,
                archived_status=archived, probe=probe,
            )
    revisions = revision_check(orchestrator, run_id, profile, frozen)
    if not revisions.get("ok"):
        return revisions
    revision_set = frozen["revision_set"]
    state = runtime.rebuild_state(profile, revision_set, target)
    if not state.get("ok"):
        return state
    if state["pending"]:
        return _failure("QUERY_REQUIRED", run_id, retry_allowed=False, intent_id=state["pending"][0])
    hashed = runtime.rebuild_input_hash(
        profile, revision_set, target, original_build_id=original,
        attempt=attempt if attempt is not None else state["next_attempt"],
        reason=reason, event_type=event_type,
    )
    if not hashed.get("ok"):
        return hashed
    return {
        "ok": True, "reason_code": "OK", "run_id": run_id, "state": position["state"],
        "module": target, "pipeline_id": hashed["binding"]["pipeline_id"],
        "original_build_id": original, "attempt": hashed["binding"]["attempt"],
        "event_type": event_type, "reason": hashed["binding"]["reason"],
        "input_hash": hashed["input_hash"], "binding": hashed["binding"],
        "probe": probe, "revisions": revisions,
        "profile": profile, "revision_set": revision_set, "position": position,
    }


def build_material(planned: dict[str, Any], original_ci_type: Any) -> dict[str, Any]:
    """What the G8 card shows and the execute step reads back. Only `binding` is hashed."""
    return {
        "binding": planned["binding"],
        "operation": "ipipe.rebuild",
        "probe": {key: planned["probe"][key] for key in ("verdict", "http_status", "reason_code")
                  if key in planned["probe"]},
        "revisions": planned["revisions"],
        "diagnosed_state": planned["state"],
        "original_ci_type": original_ci_type if isinstance(original_ci_type, str) else None,
    }


@guard_execution
def prepare_g8(
    orchestrator: Any, runtime: Any, run_id: str, *, reason: Any, module: Any = None,
    event_type: Any = DEFAULT_EVENT_TYPE,
) -> dict[str, Any]:
    """Pin the exact material and hand the hash to the approval channels. Executes nothing."""
    planned = plan(orchestrator, runtime, run_id, reason=reason, module=module, event_type=event_type)
    if not planned.get("ok"):
        return planned
    from persistence_policy import redact_secrets

    material = redact_secrets(
        build_material(planned, runtime.build_ci_type(planned["original_build_id"])))
    key = material_key(run_id, planned["input_hash"])
    try:
        orchestrator.state.save_idempotency_result(key, material)
    except ValueError:
        # The probe may have answered 403 once and 404 now; the card was built from the
        # first material and the hash does not depend on it.
        material = orchestrator.state.idempotency_result(key) or material
    # B-21: a 403/404 probe only proves the old build cannot be re-run from its
    # products; it is not evidence of why the build failed. It stays in the G8 material
    # and is never recorded as authoritative environment evidence.
    return {"ok": False, "reason_code": "APPROVAL_REQUIRED", "run_id": run_id, "gate": "G8",
            "approval_input_hash": planned["input_hash"], "controller": "ipipe",
            "module": planned["module"], "material": material}


def public_plan(planned: dict[str, Any]) -> dict[str, Any]:
    """The plan without the in-memory objects the execute step needs."""
    return {key: value for key, value in planned.items()
            if key not in {"profile", "revision_set", "position"}}


def execute(
    orchestrator: Any, runtime: Any, run_id: str, approval_id: str, input_hash: str,
    *, reconcile_only: bool = False,
) -> dict[str, Any]:
    """Trigger (or settle) the approved re-build, then close the approval's resume handoff."""
    result = _execute(
        orchestrator, runtime, run_id, approval_id, input_hash, reconcile_only=reconcile_only
    )
    if result.get("ok") and result.get("approval_id") == approval_id:
        # The rebind appended an IPIPE event, so the handoff `approve` recorded against
        # the older event can never resume anything; leaving it open made `continue`
        # fail with MULTIPLE_RESUME_HANDOFFS.
        orchestrator.state.complete_approval_handoffs(
            run_id, approval_id, actor="ipipe-rebuild", reason="APPROVAL_CONSUMED"
        )
    return result


@guard_execution
def _execute(
    orchestrator: Any, runtime: Any, run_id: str, approval_id: str, input_hash: str,
    *, reconcile_only: bool = False,
) -> dict[str, Any]:
    """Trigger (or settle) the approved re-build, then rebind the run to the new build."""
    material = orchestrator.state.idempotency_result(material_key(run_id, input_hash))
    binding = material.get("binding") if isinstance(material, dict) else None
    if (not isinstance(binding, dict) or material.get("operation") != "ipipe.rebuild"
            or binding.get("run_id") != run_id):
        return _failure("REBUILD_MATERIAL_MISSING", run_id, retry_allowed=False)
    replay = orchestrator.state.idempotency_result(f"{_REBIND_KEY}:{run_id}:{approval_id}")
    if replay is not None:
        return {**replay, "reason_code": "ALREADY_REBOUND"}
    approval = {"approval_id": approval_id, "input_hash": input_hash}
    arguments = {
        "module": binding["module"], "original_build_id": binding["original_build_id"],
        "attempt": binding["attempt"], "reason": binding["reason"],
        "event_type": binding["event_type"],
    }
    position = _position(orchestrator, run_id)
    if not position.get("ok"):
        return position
    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return pinned
    try:
        frozen = frozen_plan(position["events"])
    except ValueError as error:
        return _failure(str(error), run_id)
    if frozen is None:
        return _failure("FROZEN_PRODUCT_PLAN_REQUIRED", run_id)
    state = runtime.rebuild_state(pinned["profile"], frozen["revision_set"], binding["module"])
    if not state.get("ok"):
        return state
    if binding["attempt"] == state["next_attempt"] and not reconcile_only:
        # Nothing was sent for this attempt yet: everything that justified the request
        # must still hold at the moment of the write, not only when it was approved.
        fresh = plan(orchestrator, runtime, run_id, reason=binding["reason"],
                     module=binding["module"], event_type=binding["event_type"],
                     attempt=binding["attempt"])
        if not fresh.get("ok"):
            return fresh
        if fresh["input_hash"] != input_hash:
            return _failure("REBUILD_BINDING_CHANGED", run_id, expected=fresh["input_hash"],
                            retry_allowed=False)
    call = runtime.reconcile_rebuild if reconcile_only else runtime.rebuild
    result = call(pinned["profile"], frozen["revision_set"], approval, **arguments)
    if not result.get("ok"):
        return result
    return rebind(orchestrator, runtime, run_id, result, material, approval_id, input_hash)


def rebind(
    orchestrator: Any, runtime: Any, run_id: str, receipt: dict[str, Any],
    material: dict[str, Any], approval_id: str, input_hash: str,
) -> dict[str, Any]:
    """Make the new build the run's build, in one transaction, without editing history.

    Appends one IPIPE event (a clone of the entry payload, so the frozen plan and the
    G7 binding every reader scans for are unchanged, plus an audit record), writes the
    superseded marker for the old build and marks the pending DIAGNOSE job SUPERSEDED.
    The old build's events, receipts, artifacts and stage bindings stay as they are.
    """
    from evidence_policy import missing_evidence
    from transition_policy import POLICY_ID, TransitionPolicy

    binding = material["binding"]
    new_build, original = str(receipt.get("build_id") or ""), binding["original_build_id"]
    result_key = f"{_REBIND_KEY}:{run_id}:{approval_id}"
    replay = orchestrator.state.idempotency_result(result_key)
    if replay is not None:
        return {**replay, "reason_code": "ALREADY_REBOUND"}
    if (not new_build or new_build == original
            or any(receipt.get(name) != binding[name] for name in
                   ("pipeline_id", "revision_set_id", "attempt", "original_build_id"))):
        return _failure("REBUILD_RECEIPT_MISMATCH", run_id, build_id=new_build, retry_allowed=False)
    before, after = material.get("original_ci_type"), receipt.get("ci_type")
    if isinstance(before, str) and isinstance(after, str) and before != after:
        # The platform built it, so the receipt stays; but a build of another kind is not
        # the build the original was, and the run does not follow it silently. The new
        # build is already bound to this run (the receipt bound it), so without a mark
        # every discover/plan/watch would see two owned builds (BUILD_AMBIGUOUS) and the
        # run would have no way forward. It is recorded as rejected -- distinct from
        # superseded: it never replaced anything -- and every reader excludes it.
        rejected = {
            "run_id": run_id, "build_id": new_build, "reason_code": "TRIGGER_MODE_MISMATCH",
            "original_build_id": original, "attempt": binding["attempt"],
            "approval_id": approval_id, "input_hash": input_hash,
            "original_ci_type": before, "new_ci_type": after,
        }
        try:
            orchestrator.state.save_idempotency_result(
                build_markers.rejected_key(run_id, new_build), rejected)
        except ValueError:
            if not build_markers.is_rejected(orchestrator.state, run_id, new_build):
                raise
        return _failure("TRIGGER_MODE_MISMATCH", run_id, build_id=new_build,
                        original_ci_type=before, new_ci_type=after, retry_allowed=False,
                        build_rejected=True,
                        next_step=("新 build 已标记为不采信（ipipe.build-rejected），原 build 仍是本 run 的 build。"
                                   "可再次 `ipipe-rebuild --request-g8` 生成 attempt+1，"
                                   "或用与原 build 一致的 --event-type 重新申请。"))
    position = _position(orchestrator, run_id)
    if not position.get("ok"):
        return {**position, "build_id": new_build, "rebind_pending": True}
    bound = runtime.bind_stages(new_build)
    if not bound.get("ok"):
        return {**bound, "build_id": new_build, "rebind_pending": True, "retry_allowed": True}
    state = position["state"]
    if state == "DIAGNOSE":
        decision = TransitionPolicy().validate("DIAGNOSE", "IPIPE")
        if not decision.get("allowed"):
            return _failure(decision.get("reason_code", "INVALID_TRANSITION"), run_id)
        reason, missing = missing_evidence("IPIPE", {
            "run_id": run_id, "input_hash": input_hash, "approved_input_hash": input_hash,
            "artifacts": ["ipipe-rebuild"], "approval_id": approval_id,
            "approval_record": orchestrator.approvals.get(approval_id),
        }, "DIAGNOSE")
        if reason:
            return _failure(reason, run_id, missing_evidence=missing)
    else:
        decision = {"allowed": True, "reason_code": "OK", "current_state": "IPIPE",
                    "next_state": "IPIPE", "policy_id": POLICY_ID, "controlled": "IPIPE_REBUILD"}
    entry = next((event for event in reversed(position["events"]) if event.get("state") == "IPIPE"), None)
    if entry is None:
        return _failure("IPIPE_ENTRY_MISSING", run_id)
    jobs = [
        job["job_id"] for job in orchestrator.state.pending_producer_jobs(run_id)
        if state == "DIAGNOSE" and (job.get("payload") or {}).get("phase") == "DIAGNOSE"
    ]
    payload = copy.deepcopy(entry["payload"])
    payload["previous_state"] = state
    payload["policy_decision"] = decision
    payload["rebuild"] = {
        "kind": "IPIPE_REBUILD", "attempt": binding["attempt"], "approval_id": approval_id,
        "approval_input_hash": input_hash, "original_build_id": original, "build_id": new_build,
        "pipeline_id": binding["pipeline_id"], "module": binding["module"],
        "revision_set_id": binding["revision_set_id"], "reason": binding["reason"],
        "event_type": binding["event_type"], "adopted": bool(receipt.get("adopted")),
        "from_state": state, "from_event_id": position["current"]["event_id"],
        "superseded_job_ids": jobs,
    }
    superseded = {
        "run_id": run_id, "build_id": original, "superseded_by": new_build,
        "attempt": binding["attempt"], "approval_id": approval_id, "input_hash": input_hash,
        "reason": binding["reason"],
    }
    committed = orchestrator.state.commit_transition_result(
        run_id, position["current"]["event_id"], "IPIPE", payload, result_key,
        {"ok": True, "reason_code": "OK", "from_state": state, "build_id": new_build,
         "superseded_build_id": original, "attempt": binding["attempt"],
         "approval_id": approval_id, "superseded_jobs": jobs},
        extra_results=[(f"ipipe.build-superseded:{run_id}:{original}", superseded)],
        supersede_jobs=jobs,
    )
    status = committed.get("status")
    if status == "REPLAY":
        return {**committed["result"], "reason_code": "ALREADY_REBOUND"}
    if status == "SOURCE_EVENT_MISMATCH":
        return _failure("RUN_MOVED", run_id, build_id=new_build, rebind_pending=True)
    if status != "COMMITTED":
        return _failure("RUN_CHANGE_CONFLICT", run_id, detail=status, build_id=new_build)
    # A presentation cache: status/brief show the newest observation per module.
    orchestrator.state.save_ipipe_monitoring(run_id, new_build, {
        "module": binding["module"], "build_id": new_build, "status": "MONITORING",
        "rebuild_of": original, "attempt": binding["attempt"], "stages": bound["stages"],
        "evidence_refs": receipt.get("evidence_refs") or [],
    })
    return {**committed["result"], "stages": bound["stages"], "next": "continue"}
