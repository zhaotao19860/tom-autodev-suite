"""Frozen run product selection. Platform capabilities must be proven by adapters."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pipeline_plan import canonical_hash

SUCCESS = {"SUCC", "SUCCESS", "SUCCEEDING", "SUCCEEDED", "PASSED", "PASS"}
# Most recent successful BranchPipeline builds searched for the frozen revision.
RELEASE_SCAN_LIMIT = 30


def failure(reason: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason, **details}


def target_for(plan: dict[str, Any], run_id: str, module: str) -> dict[str, Any]:
    if (plan.get("run_id") != run_id or plan.get("plan_hash") != canonical_hash(
        {k: v for k, v in plan.items() if k != "plan_hash"}
    )):
        raise ValueError("PIPELINE_PLAN_INVALID")
    product = (plan.get("product_inputs") or {}).get(module)
    if product is not None:
        if (not isinstance(product.get("changed"), bool) or not product.get("branch")
                or not product.get("revision")):
            raise ValueError("PRODUCT_CHANGE_STATUS_UNKNOWN")
        changed = product["changed"]
        expected_name = "ChangePipeline" if changed else "BranchPipeline"
        if product.get("pipeline_name") != expected_name:
            raise ValueError("PRODUCT_PIPELINE_IDENTITY_UNVERIFIED")
        return {"run_id": run_id, "module": module, "branch": product["branch"],
                "revision": product["revision"], "changed": changed,
                "pipeline_name": expected_name, "plan_hash": plan["plan_hash"]}
    changed = module in plan.get("modules", {})
    baseline = plan.get("unchanged", {}).get(module)
    if changed == bool(baseline):
        raise ValueError("PRODUCT_CHANGE_STATUS_UNKNOWN")
    entry = plan["modules"][module] if changed else baseline
    branch = entry.get("target_branch") if changed else entry.get("branch")
    revision = entry.get("expected_revisions", {}).get(module) if changed else entry.get("revision")
    if not branch or not revision:
        raise ValueError("SOURCE_REVISION_REQUIRED")
    return {"run_id": run_id, "module": module, "branch": branch, "revision": revision,
            "changed": changed, "pipeline_name": "ChangePipeline" if changed else "BranchPipeline",
            "plan_hash": plan["plan_hash"]}


def select_product(api: Any, target: dict[str, Any], pipeline_id: str,
                   builds: list[dict[str, Any]], *, owned_build_ids: set[str],
                   compile_fallback_ids: set[str] | None = None) -> dict[str, Any]:
    """Only successful release jobs for baseline; only owned exact compile for changes.

    A baseline release must have been built from exactly the frozen revision. The newest
    successful release is not a substitute: the branch may have moved on since the run froze.
    """
    scanned = 0
    seen_revisions: list[str] = []
    # Exact-revision candidates whose product could not be downloaded. Each is checked in
    # order; one failing (an expired product, a transient 5xx) does not hide a later
    # exact candidate that still downloads. Only status codes and ids are kept.
    download_failures: list[dict[str, Any]] = []
    for build in builds:
        build_id = str(build.get("id") or build.get("pipelineBuildId") or "")
        if (not build_id or str(build.get("pipelineConfId")) != pipeline_id
                or build.get("module") != target["module"] or build.get("branch") != target["branch"]):
            continue
        if (not target["changed"] and build_id not in (compile_fallback_ids or set())):
            if build.get("status") is not None and build.get("status") not in SUCCESS:
                continue
            if scanned >= RELEASE_SCAN_LIMIT:
                break
            scanned += 1
        revision = (build.get("revision") or (build.get("trigger") or {}).get("revision")
                    or (build.get("revisions") or {}).get(target["module"]))
        changed = target["changed"]
        fallback = build_id in (compile_fallback_ids or set())
        if (changed or fallback) and (build_id not in owned_build_ids or revision != target["revision"]):
            continue
        kind = "AGILE_COMPILE" if changed or fallback else "AGILE_RELEASE"
        for stage in api.pipeline_stage_info(build_id):
            if stage.get("status") not in SUCCESS:
                continue
            stage_id = str(stage.get("id") or stage.get("stageBuildId") or "")
            if not stage_id:
                continue
            detail = api.stage_detail(stage_id)
            entity = detail.get("entities", detail)
            identity = entity.get("stageBuild") or {}
            if (str(identity.get("pipelineBuildId")) != build_id
                    or str(identity.get("pipelineConfId")) != pipeline_id
                    or str(identity.get("stageBuildId")) != stage_id
                    or identity.get("status") not in SUCCESS):
                continue
            listing = {str(j.get("id")): j for j in stage.get("jobBuildBeans", [])}
            for group in entity.get("realJobBuilds", []):
                for job in group:
                    job_id = str(job.get("id") or "")
                    listed = listing.get(job_id, {})
                    if (job.get("jobType") != kind or job.get("status") not in SUCCESS
                            or listed.get("status") not in SUCCESS or listed.get("jobType") != kind):
                        continue
                    real = job.get("realJobBuild") or {}
                    # Compile details measured on-platform omit revision; the owned build's
                    # authoritative trigger revision proves it. Release details expose the
                    # actual published source revision and must supply that field themselves.
                    job_revision = real.get("revision") or (revision if kind == "AGILE_COMPILE" else None)
                    job_branch = real.get("branch")
                    if not job_revision or job_branch != target["branch"] or real.get("module") != target["module"]:
                        continue
                    if revision and revision != job_revision:
                        continue
                    if kind == "AGILE_RELEASE" and job_revision not in seen_revisions:
                        seen_revisions.append(job_revision)
                    # Exact match against the frozen revision for every product kind.
                    if job_revision != target["revision"]:
                        continue
                    field = "productHttpUrl" if kind == "AGILE_COMPILE" else "releaseOutputHttpUrl"
                    url = real.get(field)
                    if not isinstance(url, str) or urlsplit(url).scheme not in {"https", "http"}:
                        continue
                    if not hasattr(api, "verify_product_download"):
                        return failure("PRODUCT_DOWNLOAD_UNVERIFIED", module=target["module"],
                                       build_id=build_id)
                    check = api.verify_product_download(url, target["module"])
                    if not isinstance(check, dict) or check.get("ok") is not True:
                        status = check.get("http_status") if isinstance(check, dict) else None
                        reason = check.get("reason_code") if isinstance(check, dict) else None
                        download_failures.append({
                            "build_id": build_id, "stage_build_id": stage_id,
                            "job_build_id": job_id,
                            **({"http_status": status} if isinstance(status, int) else {}),
                            **({"reason_code": reason} if isinstance(reason, str) else {}),
                        })
                        continue
                    return {"ok": True, "reason_code": "OK", **target, "revision": job_revision,
                            "pipeline_id": pipeline_id, "build_id": build_id,
                            "stage_build_id": stage_id, "job_build_id": job_id,
                            "product_url": url, "product_kind": kind,
                            "frozen_revision": target["revision"],
                            "download_verified": True}
    if download_failures:
        statuses = sorted({item["http_status"] for item in download_failures if "http_status" in item})
        return failure("PRODUCT_DOWNLOAD_UNVERIFIED", module=target["module"],
                       build_id=download_failures[0]["build_id"],
                       candidates=download_failures, http_statuses=statuses,
                       download_check=download_failures[-1])
    if seen_revisions:
        return failure("PRODUCT_REVISION_MISMATCH", **target,
                       seen_revisions=[r[:8] for r in seen_revisions],
                       scanned_builds=scanned)
    return failure("PRODUCT_URL_NOT_PUBLISHED", **target)


def compile_binding(target: dict[str, Any], pipeline_id: str,
                    capability: dict[str, Any]) -> dict[str, Any]:
    """Reviewable authorization for one adapter-proven compile-only operation."""
    if (capability.get("supported") is not True or capability.get("release_excluded") is not True
            or not capability.get("evidence_ref") or not capability.get("contract_hash")):
        raise ValueError("COMPILE_ONLY_TRIGGER_UNSUPPORTED")
    if target["changed"]:
        raise ValueError("BRANCH_COMPILE_FOR_CHANGED_FORBIDDEN")
    return {**target, "operation": "ipipe.branch_compile", "pipeline_id": pipeline_id,
            "scope": "compile_only", "release_allowed": False, "parameters": {},
            "capability": capability}


def ensure_compile(runtime: Any, target: dict[str, Any], pipeline_id: str,
                   approval: dict[str, Any]) -> dict[str, Any]:
    """Intent before write; unknown outcome only reconciles, never sends again."""
    from clients.ipipe_runtime import _approved_record
    try:
        binding = compile_binding(target, pipeline_id, runtime.api.compile_only_capability())
    except ValueError as error:
        return failure(str(error))
    digest = canonical_hash(binding)
    denied = _approved_record(runtime.approvals, approval, run_id=runtime.run_id,
                              action="G8", input_hash=digest)
    if denied:
        return denied
    blocked = runtime._profile_error()
    if blocked:
        return blocked
    key = f"ipipe.branch-compile:{runtime.run_id}:{digest}"
    payload = {**binding, "input_hash": digest, "approval_id": approval["approval_id"]}
    claim = runtime.state.claim_intent(runtime.run_id, "ipipe.branch_compile", key, payload)
    if claim["status"] == "CONFLICT":
        return failure("BRANCH_COMPILE_CONFLICT")
    completed = runtime.state.result_by_idempotency_key(key)
    if completed:
        return completed["receipt"]["response"]
    intent = claim["intent"]
    # This adapter contract is deliberately not an invented HTTP endpoint. An adapter
    # must prove exact branch/revision, compile-only scope and correlation to this intent.
    try:
        result = runtime.api.reconcile_branch_compile(binding, intent["intent_id"])
        if not result and claim["status"] != "EXISTING":
            result = runtime.api.trigger_branch_compile(binding, intent["intent_id"])
    except Exception:
        return failure("BRANCH_COMPILE_RESULT_UNKNOWN", intent_id=intent["intent_id"],
                       retry_allowed=False)
    if (not isinstance(result, dict) or result.get("binding_hash") != digest
            or result.get("intent_id") != intent["intent_id"] or not result.get("build_id")
            or any(result.get(k) != binding[k] for k in (
                "module", "branch", "revision", "pipeline_id", "scope", "release_allowed"))):
        return failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
    response = {"ok": True, "reason_code": "OK", "binding": binding,
                **{k: result[k] for k in (
                    "build_id", "binding_hash", "intent_id", "module", "branch", "revision",
                    "pipeline_id", "scope", "release_allowed")}}
    runtime.state.receipt(intent["intent_id"], response, [])
    return response
