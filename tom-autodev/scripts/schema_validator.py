from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from requirement_snapshot import (
    AcceptanceValueError,
    normalized_acceptance_ids,
    validation_error as requirement_snapshot_error,
)


@dataclass(frozen=True)
class SchemaIssue:
    path: str
    kind: str


def load_project_profile_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / "project-profile.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


_NAMED_SCHEMAS = frozenset(
    {
        "requirement-snapshot",
        "decision-log",
        "spec",
        "task-dag",
        "task-plan",
        "change-set",
        "review",
        "diagnosis",
        "ipipe-evidence",
        "run-summary",
        "optimization-proposal",
        # The submit descriptor. Deliberately not called "change-set": that name is
        # already the IMPLEMENT phase content, and the descriptor is a different
        # document stored under an artifact kind that happens to reuse the word.
        "submit-descriptor",
    }
)


def load_named_schema(name: str) -> dict[str, Any]:
    if name not in _NAMED_SCHEMAS:
        raise ValueError("SCHEMA_NAME_INVALID")
    path = Path(__file__).resolve().parents[1] / "schemas" / f"{name}.schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("SCHEMA_LOAD_FAILED") from error


def validate_named_schema(instance: Any, name: str) -> list[SchemaIssue]:
    issues = validate_schema(instance, load_named_schema(name))
    if name == "requirement-snapshot" and isinstance(instance, dict) and not issues:
        snapshot_error = requirement_snapshot_error(instance, instance.get("canonical_card_id"))
        if snapshot_error == "ICAFE_SNAPSHOT_HASH_MISMATCH":
            issues.append(SchemaIssue("content_hash", "mismatch"))
        try:
            normalized_acceptance_ids(instance.get("acceptance"))
        except AcceptanceValueError as error:
            path = "acceptance" if error.index < 0 else f"acceptance[{error.index}]"
            issues.append(SchemaIssue(path, error.kind))
    if name == "task-dag" and isinstance(instance, dict):
        issues.extend(_validate_task_dag(instance))
    if name == "decision-log" and isinstance(instance, dict):
        issues.extend(_validate_decision_log(instance))
    if name == "spec" and isinstance(instance, dict):
        issues.extend(_validate_spec(instance))
    if name == "task-plan" and isinstance(instance, dict):
        issues.extend(_validate_task_plan(instance))
    if name == "change-set" and isinstance(instance, dict):
        issues.extend(_validate_change_set(instance))
    if name == "review" and isinstance(instance, dict):
        issues.extend(_validate_review(instance))
    if name == "diagnosis" and isinstance(instance, dict):
        issues.extend(_validate_diagnosis(instance))
    if name == "ipipe-evidence" and isinstance(instance, dict):
        issues.extend(_validate_ipipe(instance))
    if name == "optimization-proposal" and isinstance(instance, dict):
        issues.extend(_validate_optimization_targets(instance))
    return sorted(set(issues), key=lambda issue: (issue.path, issue.kind))


def validate_schema(instance: Any, schema: dict[str, Any] | None = None) -> list[SchemaIssue]:
    schema = schema or load_project_profile_schema()
    issues: list[SchemaIssue] = []
    _validate(instance, schema, schema, "", issues)
    return sorted(issues, key=lambda issue: (issue.path, issue.kind))


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str, issues: list[SchemaIssue]) -> None:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            issues.append(SchemaIssue(_display(path), "invalid"))
            return
        schema = root["$defs"][reference.rsplit("/", 1)[-1]]

    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, expected_type):
        issues.append(SchemaIssue(_display(path), "invalid"))
        return

    if isinstance(value, dict):
        for field in sorted(schema.get("required", [])):
            child_path = _join(path, field)
            if field not in value:
                issues.append(SchemaIssue(child_path, "missing"))
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        if additional is False:
            for field in sorted(set(value) - set(properties)):
                issues.append(SchemaIssue(_join(path, field), "invalid"))
        elif isinstance(additional, dict):
            for field in sorted(set(value) - set(properties)):
                _validate(value[field], additional, root, _join(path, field), issues)
        for field, child_schema in properties.items():
            if field in value:
                _validate(value[field], child_schema, root, _join(path, field), issues)
        if schema.get("minProperties") and len(value) < schema["minProperties"]:
            issues.append(SchemaIssue(_display(path), "invalid"))
        return

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            issues.append(SchemaIssue(_display(path), "missing"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            issues.append(SchemaIssue(_display(path), "invalid"))
        if schema.get("uniqueItems") and len({_canonical(item) for item in value}) != len(value):
            issues.append(SchemaIssue(_display(path), "invalid"))
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]", issues)
        return

    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        issues.append(SchemaIssue(_display(path), "missing"))
    if isinstance(value, str) and "pattern" in schema:
        try:
            matches = re.search(schema["pattern"], value) is not None
        except (TypeError, re.error):
            matches = False
        if not matches:
            issues.append(SchemaIssue(_display(path), "invalid"))
    if "enum" in schema and value not in schema["enum"]:
        issues.append(SchemaIssue(_display(path), "invalid"))
    if "const" in schema and value != schema["const"]:
        issues.append(SchemaIssue(_display(path), "invalid"))
    if "minimum" in schema and value < schema["minimum"]:
        issues.append(SchemaIssue(_display(path), "invalid"))


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, candidate) for candidate in expected)
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    expected_class = types.get(expected)
    return expected_class is not None and isinstance(value, expected_class) and not (
        expected in {"integer", "number"} and isinstance(value, bool)
    )


def _validate_task_dag(instance: dict[str, Any]) -> list[SchemaIssue]:
    nodes = instance.get("nodes")
    edges = instance.get("edges")
    coverage = instance.get("acceptance_coverage")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(coverage, list):
        return []
    node_ids = {
        node.get("task_id") for node in nodes
        if isinstance(node, dict) and isinstance(node.get("task_id"), str)
    }
    issues: list[SchemaIssue] = []
    seen: set[str] = set()
    for index, node in enumerate(nodes):
        task_id = node.get("task_id") if isinstance(node, dict) else None
        if isinstance(task_id, str) and task_id in seen:
            issues.append(SchemaIssue(f"nodes[{index}].task_id", "duplicate"))
        elif isinstance(task_id, str):
            seen.add(task_id)
    adjacency = {node_id: set() for node_id in node_ids}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            continue
        source, target = edge.get("from"), edge.get("to")
        if source not in node_ids:
            issues.append(SchemaIssue(f"edges[{index}].from", "unknown"))
        if target not in node_ids:
            issues.append(SchemaIssue(f"edges[{index}].to", "unknown"))
        if source in node_ids and target in node_ids:
            adjacency[source].add(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        cyclic = any(visit(child) for child in adjacency[node_id])
        visiting.remove(node_id)
        visited.add(node_id)
        return cyclic

    if any(visit(node_id) for node_id in sorted(node_ids)):
        issues.append(SchemaIssue("edges", "cycle"))
    declared = {
        point
        for node in nodes if isinstance(node, dict)
        for point in node.get("acceptance_point_ids", []) if isinstance(point, str)
    }
    covered = {
        item.get("acceptance_point_id")
        for item in coverage if isinstance(item, dict) and item.get("task_ids")
    }
    coverage_tasks = {
        task
        for item in coverage if isinstance(item, dict)
        for task in item.get("task_ids", []) if isinstance(task, str)
    }
    if declared != covered or not coverage_tasks.issubset(node_ids):
        issues.append(SchemaIssue("acceptance_coverage", "incomplete"))
    declarations = {
        (point, node.get("task_id"))
        for node in nodes if isinstance(node, dict)
        for point in node.get("acceptance_point_ids", []) if isinstance(point, str)
    }
    for index, item in enumerate(coverage):
        if not isinstance(item, dict):
            continue
        point = item.get("acceptance_point_id")
        for task_index, task_id in enumerate(item.get("task_ids", [])):
            if (point, task_id) not in declarations:
                issues.append(SchemaIssue(f"acceptance_coverage[{index}].task_ids[{task_index}]", "inconsistent"))
    return issues


def _validate_decision_log(instance: dict[str, Any]) -> list[SchemaIssue]:
    result = instance.get("decision_result")
    decisions = instance.get("decisions")
    frontier = instance.get("unresolved_frontier")
    status = instance.get("status")
    issues: list[SchemaIssue] = []
    if result == "NO_OPEN_DECISIONS" and (decisions or frontier or status != "COMPLETE"):
        issues.append(SchemaIssue("decision_result", "inconsistent"))
    if result == "DECISIONS_RECORDED" and (not decisions or frontier or status != "COMPLETE"):
        issues.append(SchemaIssue("decision_result", "inconsistent"))
    if result == "UNRESOLVED" and (not frontier or status != "INCOMPLETE"):
        issues.append(SchemaIssue("decision_result", "inconsistent"))
    seen_ids: set[str] = set()
    if isinstance(decisions, list):
        for index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            decision_id = decision.get("id")
            if isinstance(decision_id, str):
                if decision_id in seen_ids:
                    issues.append(SchemaIssue(f"decisions[{index}].id", "duplicate"))
                seen_ids.add(decision_id)
            options = decision.get("options")
            if isinstance(options, list):
                if len(options) != len(set(options)):
                    issues.append(SchemaIssue(f"decisions[{index}].options", "duplicate"))
                if decision.get("choice") not in options:
                    issues.append(SchemaIssue(f"decisions[{index}].choice", "unknown"))
    acceptance_delta = instance.get("acceptance_delta")
    if isinstance(acceptance_delta, list):
        try:
            normalized_acceptance_ids(acceptance_delta)
        except AcceptanceValueError as error:
            path = "acceptance_delta" if error.index < 0 else f"acceptance_delta[{error.index}].id"
            issues.append(SchemaIssue(path, error.kind))
    return issues


def _validate_spec(instance: dict[str, Any]) -> list[SchemaIssue]:
    required_sections = (
        "boundaries", "errors", "compatibility", "test_interface",
        "environment_requirements", "non_goals", "risks", "rollback", "release_evidence",
    )
    issues = [
        SchemaIssue(field, "missing")
        for field in required_sections
        if isinstance(instance.get(field), list) and not instance[field]
    ]
    behaviors = instance.get("behaviors")
    scenarios = instance.get("acceptance_scenarios")
    traceability = instance.get("traceability")
    if not all(isinstance(value, list) for value in (behaviors, scenarios, traceability)):
        return issues
    behavior_ids = {
        behavior.get("id") for behavior in behaviors if isinstance(behavior, dict)
    }
    scenario_ids = {
        scenario.get("id") for scenario in scenarios if isinstance(scenario, dict)
    }
    scenario_points = {
        (scenario.get("id"), point)
        for scenario in scenarios if isinstance(scenario, dict)
        for point in scenario.get("acceptance_point_ids", [])
    }
    for index, trace in enumerate(traceability):
        if not isinstance(trace, dict):
            continue
        for child_index, behavior_id in enumerate(trace.get("behavior_ids", [])):
            if behavior_id not in behavior_ids:
                issues.append(SchemaIssue(f"traceability[{index}].behavior_ids[{child_index}]", "unknown"))
        point = trace.get("acceptance_point_id")
        for child_index, scenario_id in enumerate(trace.get("scenario_ids", [])):
            if scenario_id not in scenario_ids or (scenario_id, point) not in scenario_points:
                issues.append(SchemaIssue(f"traceability[{index}].scenario_ids[{child_index}]", "unknown"))
    return issues


def _validate_task_plan(instance: dict[str, Any]) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    repositories = instance.get("repositories")
    if isinstance(repositories, list):
        roles = [entry.get("role") for entry in repositories if isinstance(entry, dict)]
        if set(roles) != {"business", "tests"} or len(roles) != 2:
            issues.append(SchemaIssue("repositories", "incomplete"))
    checklist = instance.get("checklist")
    if isinstance(checklist, list):
        orders = [entry.get("order") for entry in checklist if isinstance(entry, dict)]
        if orders != list(range(1, len(checklist) + 1)):
            issues.append(SchemaIssue("checklist", "unordered"))
    return issues


def _validate_change_set(instance: dict[str, Any]) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    if all(isinstance(instance.get(field), str) for field in ("business_patch", "test_patch")):
        expected_diff = _canonical_hash({
            "business_patch": instance["business_patch"],
            "test_patch": instance["test_patch"],
        })
        if instance.get("full_diff_hash") != expected_diff:
            issues.append(SchemaIssue("full_diff_hash", "mismatch"))
    expected_candidate = _canonical_hash({
        key: value for key, value in instance.items() if key != "candidate_hash"
    })
    if instance.get("candidate_hash") != expected_candidate:
        issues.append(SchemaIssue("candidate_hash", "mismatch"))
    return issues


def _validate_review(instance: dict[str, Any]) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    findings = instance.get("findings")
    axes = instance.get("axes")
    verdict = instance.get("verdict")
    completeness = instance.get("completeness_state")
    if not isinstance(findings, list) or not isinstance(axes, dict):
        return issues
    finding_ids = {finding.get("id") for finding in findings if isinstance(finding, dict)}
    for axis_name in ("standards", "spec"):
        axis = axes.get(axis_name)
        if not isinstance(axis, dict):
            continue
        expected = {
            finding.get("id") for finding in findings
            if isinstance(finding, dict) and finding.get("axis") == axis_name
        }
        if set(axis.get("finding_ids", [])) != expected or not set(axis.get("finding_ids", [])).issubset(finding_ids):
            issues.append(SchemaIssue(f"axes.{axis_name}.finding_ids", "inconsistent"))
    blocking = any(isinstance(finding, dict) and finding.get("blocking") for finding in findings)
    axes_complete = all(isinstance(axes.get(name), dict) and axes[name].get("complete") is True for name in ("standards", "spec"))
    issues.extend(_validate_finding_classifications(findings))
    unclarified = any(
        isinstance(finding, dict) and finding.get("classification") == "NEEDS_CLARIFICATION"
        for finding in findings
    )
    if (
        verdict == "ACCEPT"
        and (blocking or unclarified or not axes_complete or completeness != "COMPLETE")
    ) or (verdict == "INCOMPLETE" and completeness != "INCOMPLETE"):
        issues.append(SchemaIssue("verdict", "inconsistent"))
    return issues


def _validate_finding_classifications(findings: list[Any]) -> list[SchemaIssue]:
    """Make a finding's disposition answer for itself.

    `CONFIRMED` says the reviewer went and looked; the other two values are claims about
    what was *not* established, and either one without a stated reason is the same
    unverified suggestion the classification exists to catch. A rejection may also not be
    `blocking`: holding the run on a finding the reviewer just said does not hold would
    leave nothing to repair.
    """
    issues: list[SchemaIssue] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        classification = finding.get("classification")
        reason = finding.get("disposition_reason")
        if classification in {"REJECTED_WITH_REASON", "NEEDS_CLARIFICATION"} and not (
            isinstance(reason, str) and reason.strip()
        ):
            issues.append(SchemaIssue(f"findings[{index}].disposition_reason", "missing"))
        if classification == "REJECTED_WITH_REASON" and finding.get("blocking") is True:
            issues.append(SchemaIssue(f"findings[{index}].blocking", "inconsistent"))
    return issues


def _validate_diagnosis(instance: dict[str, Any]) -> list[SchemaIssue]:
    if instance.get("evidence_state") == "INSUFFICIENT":
        if instance.get("route") != "DIAGNOSIS_INCOMPLETE":
            return [SchemaIssue("route", "inconsistent")]
        if any(instance.get(field) not in {None} for field in ("hypothesis", "repair_direction", "repair_diff_hash")):
            return [SchemaIssue("evidence_state", "inconsistent")]
        if instance.get("repair_plan") != []:
            return [SchemaIssue("repair_plan", "inconsistent")]
        return []
    required = ("hypothesis", "repair_direction")
    if any(not isinstance(instance.get(field), str) or not instance[field] for field in required):
        return [SchemaIssue("evidence_state", "inconsistent")]
    # A diff hash is the receipt that a code repair has already been written, so it is
    # required exactly when the diagnosis routes to REPAIR. The other routes decide a
    # document first -- a Spec amendment, an architecture review, a stop -- and no diff
    # exists yet at that moment. Demanding one there made every amendment-route diagnosis
    # unrepresentable, which is why the field is nullable in the schema itself.
    if instance.get("route") == "REPAIR":
        diff_hash = instance.get("repair_diff_hash")
        if not isinstance(diff_hash, str) or not diff_hash:
            return [SchemaIssue("repair_diff_hash", "missing")]
    elif instance.get("repair_diff_hash") is not None:
        return [SchemaIssue("repair_diff_hash", "inconsistent")]
    if not instance.get("repair_plan"):
        return [SchemaIssue("repair_plan", "missing")]
    return []


def _validate_ipipe(instance: dict[str, Any]) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    status = instance.get("status")
    signature = instance.get("failure_signature")
    if (status == "SUCCESS" and signature is not None) or (
        status == "FAILURE" and not isinstance(signature, str)
    ):
        issues.append(SchemaIssue("failure_signature", "inconsistent"))
    if status == "SUCCESS" and not instance.get("release_evidence"):
        issues.append(SchemaIssue("release_evidence", "missing"))
    jobs = instance.get("jobs")
    job_ids = {
        job.get("job_id") for job in jobs or []
        if isinstance(job, dict) and isinstance(job.get("job_id"), str)
    } if isinstance(jobs, list) else set()
    stages = instance.get("stages")
    statuses = []
    if isinstance(stages, list):
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            statuses.append(stage.get("status"))
            for job_index, job_id in enumerate(stage.get("job_ids", [])):
                if job_id not in job_ids:
                    issues.append(SchemaIssue(f"stages[{stage_index}].job_ids[{job_index}]", "unknown"))
    if isinstance(jobs, list):
        statuses.extend(job.get("status") for job in jobs if isinstance(job, dict))
        for index, job in enumerate(jobs):
            if not isinstance(job, dict):
                continue
            try:
                from persistence_policy import validate_evidence_refs

                validate_evidence_refs(job.get("evidence_refs"))
            except ValueError:
                issues.append(SchemaIssue(f"jobs[{index}].evidence_refs", "invalid"))
    try:
        from persistence_policy import validate_evidence_refs

        validate_evidence_refs(instance.get("remote_evidence_refs"))
    except ValueError:
        issues.append(SchemaIssue("remote_evidence_refs", "invalid"))
    if (
        status == "SUCCESS" and any(item != "SUCCESS" for item in statuses)
    ) or (
        status == "FAILURE" and "FAILURE" not in statuses
    ) or (
        status == "BLOCKED" and "BLOCKED" not in statuses
    ):
        issues.append(SchemaIssue("status", "inconsistent"))
    return issues


def _validate_optimization_targets(instance: dict[str, Any]) -> list[SchemaIssue]:
    """Refuse a G10 candidate that names a file G10 must never touch.

    The targets live at `candidate.target_files[].path` and are resolved absolute
    paths. This check read a top-level `target_files` of relative path strings and
    required each to start with `scripts/` or a skill directory -- a shape the
    proposal builder has never produced, so every real target would have been
    flagged as forbidden had anything ever loaded this schema.

    The relative-prefix rule is gone rather than rewritten: confinement is decided in
    `_normalize_candidate`, which resolves each path and checks containment in the
    approved roots, and a substring test on an absolute path cannot improve on that.
    What survives is the deny list, which does compose -- it names files that sit
    inside an approved root and still must not be rewritten by an automated proposal.
    """
    candidate = instance.get("candidate")
    targets = candidate.get("target_files") if isinstance(candidate, dict) else None
    if not isinstance(targets, list):
        return []
    forbidden = ("profiles/", "project-profile", "pipeline", "credential", "state.sqlite", "approvals.sqlite")
    issues = []
    for index, target in enumerate(targets):
        path = target.get("path") if isinstance(target, dict) else None
        normalized = path.replace("\\", "/").lower() if isinstance(path, str) else ""
        if not normalized or any(fragment in normalized for fragment in forbidden):
            issues.append(SchemaIssue(f"candidate.target_files[{index}].path", "forbidden"))
    return issues


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _join(path: str, child: str) -> str:
    return child if not path else f"{path}.{child}"


def _display(path: str) -> str:
    return path or "profile"
