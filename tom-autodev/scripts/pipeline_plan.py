"""One frozen, reviewed input identity shared by build scheduling and release.

A missing dependency declaration means all repositories. An explicit depends_on
list narrows it; the module itself and the shared product-test repository are
always required. No live CR query is allowed to change an approved plan.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from typing import Any


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _decode(artifact: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(artifact["content"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("SUBMISSION_CONTENT_INVALID") from None
    if not isinstance(value, dict):
        raise ValueError("SUBMISSION_CONTENT_INVALID")
    return value


def current_descriptors(orchestrator: Any, run_id: str) -> dict[str, dict[str, Any]]:
    """Current PASS descriptor per task, preserving review order, including revision id."""
    current: dict[str, dict[str, Any]] = {}
    protocol = orchestrator.phase_protocol()
    dag = orchestrator.artifacts.latest_phase(run_id, "TASKS", None)
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        if not artifact.get("valid"):
            raise ValueError("ARTIFACT_INTEGRITY_FAILED")
        if artifact.get("kind") != "change-set":
            continue
        metadata = artifact.get("metadata") or {}
        task = metadata.get("task_id")
        if metadata.get("verdict") != "PASS" or not isinstance(task, str) or not task:
            continue
        if dag.get("valid") and not protocol._task_reviewed(run_id, task):
            continue
        review = orchestrator.artifacts.latest_phase(run_id, "REVIEW", task)
        if review.get("valid") and metadata.get("reviewed_artifact_id") != review["artifact_id"]:
            # A fresh PASS does not authorize the preceding descriptor after a crash
            # between committing Review and archiving its new submit descriptor.
            continue
        descriptor = _decode(artifact)
        current.pop(task, None)
        current[task] = {"descriptor": descriptor, "artifact": artifact}
    return current


def current_submissions(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    """Select by (current reviewed Change Set, revision set), never by ID ordering."""
    current = current_descriptors(orchestrator, run_id)
    identities = [(entry["descriptor"].get("change_set_id"),
                   entry["descriptor"].get("revision_set_id")) for entry in current.values()]
    found: dict[tuple[Any, Any], dict[str, Any]] = {}
    artifacts = orchestrator.artifacts.artifacts_for_run(run_id)
    strict = bool(current) or any(artifact.get("kind") == "change-set" for artifact in artifacts)
    for artifact in artifacts:
        if not artifact.get("valid"):
            raise ValueError("ARTIFACT_INTEGRITY_FAILED")
        if artifact.get("kind") != "submission":
            continue
        metadata = artifact.get("metadata") or {}
        identity = (metadata.get("change_set_id"), metadata.get("revision_set_id"))
        if strict and identity not in identities:
            continue
        binding = metadata.get("controller_binding") or {}
        found[identity] = {
            "artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"],
            "change_set_id": identity[0], "revision_set_id": identity[1],
            "controller_binding": binding,
        }
    if strict:
        return [found[key] for key in dict.fromkeys(identities) if key in found]
    # Legacy descriptor-free runs retain chronological order, not lexical ID order.
    return list(found.values())


def required_modules(profile: dict[str, Any]) -> list[str]:
    entries = profile.get("pipeline_profile", {}).get("pipelines") or []
    required = [entry["module"] for entry in entries if entry.get("required_for_release")]
    return required or [profile["business_repos"][0]["module"]]


def create_plan(orchestrator: Any, run_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    submissions = current_submissions(orchestrator, run_id)
    descriptors = current_descriptors(orchestrator, run_id)
    recorded = {(s["change_set_id"], s["revision_set_id"]) for s in submissions}
    if any((d["descriptor"]["change_set_id"], d["descriptor"]["revision_set_id"]) not in recorded
           for d in descriptors.values()):
        raise ValueError("SUBMISSION_REQUIRED")
    repositories = [("business", r) for r in profile["business_repos"]]
    repositories.append(("test", profile["test_repo"]))
    known = {r["module"]: r for _, r in repositories}
    revisions: dict[str, str] = {}
    candidates: dict[str, set[str]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    test_business_module = None
    decoded_submissions = []
    for submission in submissions:
        artifact = orchestrator.artifacts.get(submission["artifact_id"])
        receipt = _decode(artifact)
        revision_set = receipt.get("revision_set")
        if not isinstance(revision_set, dict) or set(revision_set) != {"business", "test"}:
            raise ValueError("SOURCE_REVISION_REQUIRED")
        for role, entry in revision_set.items():
            if not isinstance(entry, dict) or entry.get("module") not in known:
                raise ValueError("SUBMISSION_BINDING_MISMATCH")
            module = entry["module"]
            if entry.get("branch") != known[module]["branch"] or not entry.get("revision"):
                raise ValueError("SOURCE_REVISION_MISMATCH")
            if role == "test" and module != profile["test_repo"]["module"]:
                raise ValueError("SUBMISSION_BINDING_MISMATCH")
            candidates.setdefault(module, set()).add(entry["revision"])
        decoded_submissions.append((submission, revision_set))
    if set(candidates) != set(known):
        raise ValueError("REVISION_UNRESOLVED")
    for module, values in candidates.items():
        if len(values) == 1:
            revisions[module] = next(iter(values))
            continue
        repository = known[module]
        path = repository.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("REVISION_AMBIGUOUS")
        maximal = []
        for candidate in sorted(values):
            if all(candidate == other or _is_ancestor(path, other, candidate)
                   for other in values):
                maximal.append(candidate)
        if len(maximal) != 1:
            raise ValueError("REVISION_AMBIGUOUS")
        revisions[module] = maximal[0]
    test_module = profile["test_repo"]["module"]
    for submission, revision_set in decoded_submissions:
        business_module = revision_set["business"]["module"]
        if revision_set["business"]["revision"] == revisions[business_module]:
            receipts[business_module] = submission
        if revision_set["test"]["revision"] == revisions[test_module]:
            receipts[test_module] = submission
            test_business_module = business_module
    revision_list = [{"kind": kind, "module": r["module"], "branch": r["branch"],
                      "revision": revisions[r["module"]]} for kind, r in repositories]
    revision_set = {"run_id": run_id, "revision_set_id": canonical_hash(revision_list),
                    "repositories": revision_list, "parameters": {}}
    pipeline = profile["pipeline_profile"]
    entries = {e["module"]: e for e in pipeline.get("pipelines") or []}
    modules = {}
    for module in required_modules(profile):
        submission = receipts.get(module)
        if submission is None:
            raise ValueError("IPIPE_MODULE_SUBMISSION_MISSING")
        entry = entries.get(module, {})
        dependencies = entry.get("depends_on", list(known))
        if not isinstance(dependencies, list) or any(d not in known for d in dependencies):
            raise ValueError("PIPELINE_DEPENDENCY_INVALID")
        dependency_set = {module, test_module, *dependencies}
        if module == test_module:
            dependency_set.add(test_business_module)
        # Dependency declarations describe inputs, so include dependencies of dependencies.
        pending = list(dependencies)
        while pending:
            dependency = pending.pop()
            if dependency == test_module:
                continue
            indirect = entries.get(dependency, {}).get("depends_on", list(known))
            if not isinstance(indirect, list) or any(d not in known for d in indirect):
                raise ValueError("PIPELINE_DEPENDENCY_INVALID")
            for item in indirect:
                if item not in dependency_set:
                    dependency_set.add(item)
                    pending.append(item)
        expected = {m: revisions[m] for m in sorted(dependency_set)}
        source = {"business": revisions[module], "tests": revisions[test_module]}
        if module == test_module:
            source["business"] = revisions[test_business_module]
        target = {
            "module": module, "pipeline_id": entry.get("pipeline_id", pipeline["pipeline_id"]),
            "release_rule": entry.get("release_rule", pipeline["release_rule"]),
            "stage_classes": entry.get("stage_classes", pipeline["stage_classes"]),
            "target_branch": known[module]["branch"],
            "environment_fingerprint": canonical_hash(profile["environment_profile"]),
            "source_revisions": source, "expected_revisions": expected,
            "expected_branches": {m: known[m]["branch"] for m in sorted(dependency_set)},
            "artifact_id": submission["artifact_id"], "sha256": submission["sha256"],
            "change_set_id": submission["change_set_id"], "revision_set_id": submission["revision_set_id"],
        }
        target["binding_hash"] = canonical_hash({key: target[key] for key in (
            "module", "pipeline_id", "release_rule", "environment_fingerprint",
            "source_revisions", "expected_revisions", "target_branch", "expected_branches", "stage_classes")})
        modules[module] = target
    plan = {"version": 1, "run_id": run_id, "revision_set": revision_set,
            "profile_content_hash": canonical_hash(profile),
            "modules": modules, "required_modules": list(modules), "submissions": submissions}
    return {**plan, "plan_hash": canonical_hash(plan)}


def _is_ancestor(path: str, old: str, new: str) -> bool:
    """Prove a revision supersedes another without consulting a live remote."""
    if not all(isinstance(ref, str) and ref and not ref.startswith("-") for ref in (old, new)):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", path, "merge-base", "--is-ancestor", old, new],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def frozen_plan(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("state") == "IPIPE":
            value = (event.get("payload") or {}).get("pipeline_plan")
            if value is None:
                return None
            if not isinstance(value, dict) or value.get("plan_hash") != canonical_hash(
                {k: v for k, v in value.items() if k != "plan_hash"}
            ):
                raise ValueError("PIPELINE_PLAN_INVALID")
            if value.get("run_id") != event.get("run_id"):
                raise ValueError("PIPELINE_PLAN_INVALID")
            if (value.get("version") != 1 or not isinstance(value.get("modules"), dict)
                    or not isinstance(value.get("required_modules"), list)
                    or not value["required_modules"]
                    or set(value["required_modules"]) != set(value["modules"])):
                raise ValueError("PIPELINE_PLAN_INVALID")
            return copy.deepcopy(value)
    return None


def build_matches(binding: Any, target: dict[str, Any]) -> bool:
    return isinstance(binding, dict) and all(
        binding.get(key) == target.get(key)
        for key in ("module", "pipeline_id", "release_rule", "environment_fingerprint", "target_branch", "stage_classes")
    ) and all((binding.get("revision_map") or {}).get(module) == revision
              for module, revision in target["expected_revisions"].items()) and all(
        {r.get("module"): r.get("branch") for r in binding.get("repositories", [])}.get(module) == branch
        for module, branch in target["expected_branches"].items())


def successful_builds(artifacts: Any, state: Any, run_id: str,
                      plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve each module to evidence AND its durable runtime binding for this plan."""
    found = {}
    for artifact in artifacts.phase_artifacts(run_id, "IPIPE"):
        if not artifact.get("valid"):
            raise ValueError("ARTIFACT_INTEGRITY_FAILED")
        envelope = artifact.get("envelope") or {}
        content = envelope.get("content") or {}
        target = plan["modules"].get(content.get("module"))
        if not target or content.get("status") != "SUCCESS":
            continue
        durable = state.idempotency_result(f"ipipe.build-binding:{run_id}:{content.get('build_id')}")
        binding = durable.get("binding") if isinstance(durable, dict) else None
        if envelope.get("parent_artifact_hash") != target["binding_hash"]:
            continue
        if (content.get("revisions") != target["source_revisions"] or any(
            content.get(key) != target[key] for key in (
                "module", "pipeline_id", "release_rule", "environment_fingerprint")
        )):
            raise ValueError("ARTIFACT_INTEGRITY_FAILED")
        if (not isinstance(durable, dict) or durable.get("run_id") != run_id
                or durable.get("build_id") != content.get("build_id")
                or not build_matches(binding, target)):
            raise ValueError("BUILD_BINDING_MISMATCH")
        found[target["module"]] = artifact
    return found


def release_builds(artifacts: Any, state: Any, run_id: str,
                   plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = successful_builds(artifacts, state, run_id, plan)
    if set(found) != set(plan["required_modules"]):
        raise ValueError("RELEASE_EVIDENCE_INCOMPLETE")
    return {module: found[module] for module in plan["required_modules"]}


def verification_key(run_id: str, build_id: str, target: dict[str, Any], release_id: str) -> str:
    return f"ipipe.release-verification:{run_id}:{build_id}:{target['binding_hash']}:{release_id}"


def release_binding_error(artifacts: Any, state: Any, run_id: str,
                          plan: dict[str, Any], content: Any) -> str | None:
    """The aggregate must prove every planned module through an owned runtime receipt."""
    if not isinstance(content, dict) or content.get("pipeline_plan_hash") != plan["plan_hash"]:
        return "PIPELINE_PLAN_MISMATCH"
    proofs = content.get("module_releases")
    if not isinstance(proofs, dict) or set(proofs) != set(plan["required_modules"]):
        return "RELEASE_EVIDENCE_INCOMPLETE"
    selected = release_builds(artifacts, state, run_id, plan)
    for module, artifact in selected.items():
        proof = proofs[module]
        ipipe = artifact["envelope"]["content"]
        if not isinstance(proof, dict):
            return "RELEASE_EVIDENCE_INCOMPLETE"
        if any(proof.get(key) != ipipe.get(key) for key in (
            "build_id", "pipeline_id", "module", "revisions", "release_rule", "environment_fingerprint"
        )):
            return "RELEASE_BINDING_MISMATCH"
        target = plan["modules"][module]
        receipt = state.idempotency_result(verification_key(
            run_id, ipipe["build_id"], target, proof.get("release_id", "")))
        if not isinstance(receipt, dict) or receipt != proof:
            return "RELEASE_VERIFICATION_REQUIRED"
    primary = proofs[plan["required_modules"][-1]]
    if any(content.get(key) != value for key, value in primary.items()
           if key not in {"release_evidence", "remote_evidence_refs"}):
        return "RELEASE_BINDING_MISMATCH"
    for key in ("release_evidence", "remote_evidence_refs"):
        expected = list(dict.fromkeys(ref for proof in proofs.values() for ref in proof[key]))
        if content.get(key) != expected:
            return "RELEASE_BINDING_MISMATCH"
    return None
