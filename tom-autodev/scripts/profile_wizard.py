"""Guided per-requirement profile authoring for tom-autodev.

A project's stable facts live in a shipped template (`templates/<project>.template.yaml`).
Starting a new requirement should only ask for what actually changes — the branches,
the knowledge-base parent, the pinned revisions, and the people — and derive everything
that follows from those. This module does that deterministic merge and writes a
per-requirement profile to `config/projects/<project>/<CARD>.yaml`, so concurrent
requirements never overwrite one another's profile.

The agent gathers the changed values (in chat) into an `answers` object; this module
fills the template, auto-derives the dependent slots, validates, and writes. It performs
no network or project execution.

answers = {
  "repository_paths": {"<module>": "<workspace checkout>", ...},
  "branches": {"<module>": "<branch>", ...},   # business repo branches
  "test_branch": "<branch>",                    # product-test repo branch
  "revisions": {"<module>": "<revision>", ...}, # pinned source revisions
  "ku": [{"repo_id": .., "parent_doc_id": .., "revision": ..}, ...],  # by order of ku sources
  "members": {"development": [...], "test": [...], "project": [...]},
  "environment_provenance": {
      "status": "VERIFIED", "runner_identity": ..., "image_toolchain_identity": ...,
      "verified_at": ..., "verifier": ..., "evidence_ref": ...
  },
}
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from project_registry import requirement_profile_path, save_profile, validate_profile

_PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


def templates_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def template_path(project: str) -> Path:
    return templates_dir() / f"{project}.template.yaml"


def _replace_workspace_root(value: Any, workspace_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace("__WORKSPACE_ROOT__", str(workspace_root))
    if isinstance(value, dict):
        return {key: _replace_workspace_root(child, workspace_root) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_workspace_root(child, workspace_root) for child in value]
    return value


def load_template(project: str, workspace_root: Path | str | None = None) -> dict[str, Any]:
    root = Path(workspace_root or Path.cwd()).expanduser().resolve()
    template = yaml.safe_load(template_path(project).read_text(encoding="utf-8"))
    return _replace_workspace_root(template, root)


def _lock_with_branch(lock: Any, branch: str) -> str:
    if isinstance(lock, str) and "/" in lock:
        return "/".join(lock.split("/")[:-1] + [branch])
    return branch


def build_profile(template: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Fill a template's variable slots from answers and derive the dependent ones."""
    profile = copy.deepcopy(template)
    repository_paths = answers.get("repository_paths") or {}
    for repo in [*profile.get("business_repos", []), profile.get("test_repo")]:
        if isinstance(repo, dict) and repo.get("module") in repository_paths:
            repo["path"] = str(Path(repository_paths[repo["module"]]).expanduser().resolve())
    branches = answers.get("branches") or {}
    revisions = answers.get("revisions") or {}

    for repo in profile.get("business_repos", []):
        module = repo.get("module")
        if module in branches:
            repo["branch"] = branches[module]
            repo["lock"] = _lock_with_branch(repo.get("lock"), branches[module])

    test_branch = answers.get("test_branch")
    test_repo = profile.get("test_repo")
    if test_branch and isinstance(test_repo, dict):
        test_repo["branch"] = test_branch
        test_repo["lock"] = _lock_with_branch(test_repo.get("lock"), test_branch)

    # A repo's revision is one fact; the source and graph views of it must not drift.
    revision_by_key: dict[str, str] = {}
    for module, revision in revisions.items():
        revision_by_key[module] = revision
        revision_by_key[module.rsplit("/", 1)[-1]] = revision

    ku_answers = list(answers.get("ku") or [])
    ku_index = 0
    for source in profile.get("knowledge_sources", []):
        provider = source.get("provider")
        if provider == "ku":
            if ku_index < len(ku_answers):
                answer = ku_answers[ku_index]
                ku_index += 1
                for key in ("repo_id", "parent_doc_id", "revision", "repository", "search_scope"):
                    if answer.get(key) is not None:
                        source[key] = answer[key]
        elif provider in ("repository", "gitnexus"):
            repo_key = source.get("repository")
            if repo_key in revision_by_key:
                source["revision"] = revision_by_key[repo_key]

    primary_branch = None
    business = profile.get("business_repos")
    if isinstance(business, list) and business:
        primary_branch = business[0].get("branch")
    pipeline = profile.get("pipeline_profile")
    if isinstance(pipeline, dict) and isinstance(pipeline.get("release_rule"), str) and primary_branch:
        pipeline["release_rule"] = pipeline["release_rule"].replace("{primary_branch}", primary_branch)

    # Pipeline ids change while stages/rules stay put, so they are discovered per module
    # (ipipe-cli) rather than hand-kept. The top-level id follows the primary business repo.
    pipeline_ids = answers.get("pipeline_ids") or {}
    if isinstance(pipeline, dict) and pipeline_ids:
        for entry in pipeline.get("pipelines", []):
            module = entry.get("module")
            if module in pipeline_ids:
                entry["pipeline_id"] = str(pipeline_ids[module])
        if isinstance(business, list) and business:
            primary_module = business[0].get("module")
            if primary_module in pipeline_ids:
                pipeline["pipeline_id"] = str(pipeline_ids[primary_module])

    members = answers.get("members") or {}
    channels = profile.get("approval_channels")
    role_members = channels.get("role_members") if isinstance(channels, dict) else None
    if isinstance(role_members, dict):
        for role in ("development", "test", "project"):
            if members.get(role):
                role_members[role] = list(members[role])
    environment_provenance = answers.get("environment_provenance")
    environment = profile.get("environment_profile")
    if isinstance(environment, dict) and isinstance(environment_provenance, dict):
        environment["provenance"] = copy.deepcopy(environment_provenance)
    return profile


def unfilled_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(unfilled_placeholders(child) for child in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(unfilled_placeholders(child) for child in value)) if value else set()
    return set()


def confirmed_inputs(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the exact variable facts a user must confirm before writing.

    Keeping this projection separate from the full profile means stable template
    data can evolve without making a user re-confirm unrelated environment text.
    The writer compares the caller's `confirmed_inputs` with this projection,
    rather than accepting a free-form boolean that could accidentally acknowledge
    values different from those written.
    """
    branches = {
        repo.get("module"): repo.get("branch")
        for repo in profile.get("business_repos", [])
        if isinstance(repo, dict)
    }
    revisions = {
        source.get("repository"): source.get("revision")
        for source in profile.get("knowledge_sources", [])
        if isinstance(source, dict)
        and source.get("provider") == "repository"
    }
    ku = [
        {
            key: source.get(key)
            for key in ("repo_id", "parent_doc_id", "revision", "repository", "search_scope")
        }
        for source in profile.get("knowledge_sources", [])
        if isinstance(source, dict) and source.get("provider") == "ku"
    ]
    pipeline_ids = {
        entry.get("module"): str(entry.get("pipeline_id"))
        for entry in (profile.get("pipeline_profile", {}).get("pipelines", []) or [])
        if isinstance(entry, dict) and entry.get("module") and entry.get("pipeline_id") is not None
    }
    members = (
        profile.get("approval_channels", {}).get("role_members", {})
        if isinstance(profile.get("approval_channels"), dict)
        else {}
    )
    environment = profile.get("environment_profile")
    provenance = environment.get("provenance") if isinstance(environment, dict) else None
    return {
        "repository_paths": {
            repo.get("module"): repo.get("path")
            for repo in [*profile.get("business_repos", []), profile.get("test_repo")]
            if isinstance(repo, dict) and repo.get("module")
        },
        "branches": branches,
        "test_branch": (profile.get("test_repo") or {}).get("branch"),
        "revisions": revisions,
        "ku": ku,
        "pipeline_ids": pipeline_ids,
        "members": {
            role: list(members.get(role, []))
            for role in ("development", "test", "project")
        },
        "environment_provenance": copy.deepcopy(provenance),
    }


def confirmation_hash(inputs: dict[str, Any]) -> str:
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git(path: str, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", path, *args], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def discover_branches(template: dict[str, Any], workspace_root: Path | str | None = None) -> dict[str, Any]:
    """Suggest branches + pinned revisions from each repo's current git checkout.

    These are defaults to confirm, not a binding: the checked-out branch may not be the
    requirement branch, so the caller shows them and lets the user override before build.
    Returns the answers-shaped `branches` / `test_branch` / `revisions`, plus `unresolved`
    for repos whose checkout could not be read.
    """
    root = Path(workspace_root or Path.cwd()).expanduser().resolve()
    candidates: list[dict[str, Any]] = []
    children = sorted(root.iterdir()) if root.is_dir() else []
    for child in children:
        if not child.is_dir():
            continue
        branch = _git(str(child), "rev-parse", "--abbrev-ref", "HEAD")
        revision = _git(str(child), "rev-parse", "HEAD")
        if branch and revision:
            candidates.append({
                "name": child.name,
                "path": str(child),
                "branch": branch,
                "revision": revision,
            })
    branches: dict[str, str] = {}
    revisions: dict[str, str] = {}
    repository_paths: dict[str, str] = {}
    unresolved: list[str] = []
    for repo in template.get("business_repos", []):
        module, path = repo.get("module"), repo.get("path")
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") if path else None
        revision = _git(path, "rev-parse", "HEAD") if path else None
        if branch and revision:
            repository_paths[module] = str(Path(path).expanduser().resolve())
            branches[module] = branch
            revisions[module] = revision
        else:
            unresolved.append(module)
    test = template.get("test_repo") or {}
    test_branch = _git(test.get("path"), "rev-parse", "--abbrev-ref", "HEAD") if test.get("path") else None
    result: dict[str, Any] = {
        "candidates": candidates,
        "repository_paths": repository_paths,
        "branches": branches,
        "revisions": revisions,
        "unresolved": unresolved,
    }
    if test_branch:
        repository_paths[test.get("module")] = str(Path(test.get("path")).expanduser().resolve())
        result["test_branch"] = test_branch
    elif test:
        unresolved.append(test.get("module"))
    return result


def _ipipe_cli() -> str | None:
    found = shutil.which("ipipe-cli")
    if found:
        return found
    candidate = Path.home() / ".ipipe-cli" / "bin" / "ipipe-cli"
    return str(candidate) if candidate.is_file() else None


def _default_pipeline_transport(cli: str, module: str, timeout_seconds: float) -> str:
    return subprocess.run(
        [cli, "pipeline", "list", "--module", module],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    ).stdout


def discover_pipeline_ids(
    modules: list[str],
    *,
    pipeline_name: str = "ChangePipeline",
    transport: Any | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Resolve each module's pipeline id from `ipipe-cli pipeline list` (config unchanged,
    only ids move). Picks the entry whose `pipelineName` matches exactly, so distractors
    like `ChangePipeline_zyd` are ignored. Modules that don't resolve are `unresolved`.

    ``transport`` is injectable for offline wizard tests and alternate authenticated
    adapters. It receives ``(cli, module, timeout_seconds)`` and returns the raw JSON
    response or a decoded mapping. The default transport is bounded and read-only.
    """
    cli = _ipipe_cli()
    pipeline_ids: dict[str, str] = {}
    unresolved: list[str] = []
    evidence: list[dict[str, Any]] = []
    if not cli:
        return {
            "pipeline_ids": {},
            "unresolved": list(modules),
            "reason_code": "IPIPE_CLI_NOT_FOUND",
            "evidence": evidence,
        }
    query = transport or _default_pipeline_transport
    for module in modules:
        try:
            raw = query(cli, module, timeout_seconds)
            payload = raw if isinstance(raw, dict) else json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
                raise ValueError("IPIPE_RESPONSE_INVALID")
            entities = payload["entities"]
            if any(not isinstance(entity, dict) for entity in entities):
                raise ValueError("IPIPE_RESPONSE_INVALID")
        except subprocess.TimeoutExpired:
            unresolved.append(module)
            evidence.append({"module": module, "status": "TIMEOUT"})
            continue
        except (OSError, subprocess.CalledProcessError):
            unresolved.append(module)
            evidence.append({"module": module, "status": "QUERY_FAILED"})
            continue
        except (TypeError, ValueError, json.JSONDecodeError):
            unresolved.append(module)
            evidence.append({"module": module, "status": "INVALID_RESPONSE"})
            continue
        matches = [e for e in entities if e.get("pipelineName") == pipeline_name]
        if len(matches) == 1 and matches[0].get("id") is not None:
            pipeline_ids[module] = str(matches[0]["id"])
            evidence.append({
                "module": module,
                "pipeline_name": pipeline_name,
                "status": "MATCHED",
                "pipeline_id": str(matches[0]["id"]),
                "candidate_count": len(matches),
            })
        else:
            unresolved.append(module)
            evidence.append({
                "module": module,
                "pipeline_name": pipeline_name,
                "status": "AMBIGUOUS" if len(matches) > 1 else "NOT_FOUND",
                "candidate_count": len(matches),
            })
    return {
        "pipeline_ids": pipeline_ids,
        "unresolved": unresolved,
        "reason_code": "OK" if not unresolved else "PIPELINE_DISCOVERY_INCOMPLETE",
        "evidence": evidence,
    }


def write_requirement_profile(
    project: str,
    card: str,
    answers: dict[str, Any],
    *,
    config_root: Path | str | None = None,
    workspace_root: Path | str | None = None,
    check_paths: bool = True,
    confirmation: bool = True,
) -> dict[str, Any]:
    profile = build_profile(load_template(project, workspace_root), answers)
    leftover = unfilled_placeholders(profile)
    if leftover:
        return {"ready": False, "reason_code": "TEMPLATE_PLACEHOLDER_UNFILLED", "placeholders": sorted(leftover)}
    expected_confirmation = confirmed_inputs(profile)
    if answers.get("confirmed_inputs") != expected_confirmation:
        return {
            "ready": False,
            "reason_code": "PROFILE_CONFIRMATION_REQUIRED",
            "confirmed_inputs": expected_confirmation,
        }
    confirmed_by = answers.get("confirmed_by")
    if not isinstance(confirmed_by, str) or not confirmed_by.strip():
        return {
            "ready": False,
            "reason_code": "PROFILE_CONFIRMATION_REQUIRED",
            "confirmed_inputs": expected_confirmation,
            "missing": ["confirmed_by"],
        }
    profile["profile_confirmation"] = {
        "confirmed_by": confirmed_by.strip(),
        "confirmed_inputs_hash": confirmation_hash(expected_confirmation),
        "source": "profile_wizard",
    }
    validation = validate_profile(profile, check_paths=check_paths)
    if not validation["ready"]:
        return validation
    path = requirement_profile_path(project, card, config_root)
    previous_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    result = save_profile(
        path,
        profile,
        previous_hash,
        confirmation,
        check_paths=check_paths,
    )
    return {**result, "card": card, "project": project}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成需求级 tom-autodev profile（只填变化的部分）")
    parser.add_argument("--project", required=True)
    parser.add_argument("--card", required=True)
    parser.add_argument("--answers", help="answers JSON 文件路径")
    parser.add_argument("--config-root")
    parser.add_argument("--workspace-root", help="包含各仓 checkout 的当前工作目录，默认当前目录")
    parser.add_argument("--no-check-paths", action="store_true")
    args = parser.parse_args(argv)
    template = load_template(args.project, args.workspace_root)
    if not args.answers:
        business_modules = [
            repo.get("module")
            for repo in template.get("business_repos", [])
            if isinstance(repo, dict) and repo.get("module")
        ]
        modules = business_modules + [
            repo.get("module")
            for repo in [template.get("test_repo")]
            if isinstance(repo, dict) and repo.get("module")
        ]
        print(json.dumps({
            "project": args.project,
            "card": args.card,
            "branch_candidates": discover_branches(template, args.workspace_root),
            "pipeline_candidates": discover_pipeline_ids(modules),
            "next": "把候选值确认或修改后写入 answers.json，并补充 confirmed_inputs，再执行 --answers",
        }, ensure_ascii=False, indent=2))
        return 0
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    result = write_requirement_profile(
        args.project,
        args.card,
        answers,
        config_root=args.config_root,
        workspace_root=args.workspace_root,
        check_paths=not args.no_check_paths,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
