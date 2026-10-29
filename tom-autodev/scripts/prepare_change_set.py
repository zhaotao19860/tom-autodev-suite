"""Deterministic, read-only producer for IMPLEMENT change-set content.

The model may describe the metadata, but it must not manufacture a patch or a
hash.  This module obtains both patches from the run-owned worktrees pinned by
the current IMPLEMENT action and its PLAN predecessor.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ValueError("GIT_COMMAND_FAILED")
    return result.stdout


def _head(root: Path) -> str:
    try:
        return _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    except (UnicodeDecodeError, ValueError):
        raise ValueError("WORKTREE_HEAD_INVALID") from None


def _require_clean(root: Path) -> None:
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=none"):
        raise ValueError("WORKTREE_DIRTY")


def _diff(root: Path, baseline: str, revision: str) -> str:
    """Compare immutable commits, not the index; never strip patch whitespace."""
    try:
        return _git(
            root, "-c", "core.quotePath=true", "-c", "diff.orderFile=/dev/null",
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
            "--no-color", "--no-renames", "--diff-algorithm=myers", "--no-indent-heuristic",
            "--src-prefix=a/", "--dst-prefix=b/", "--unified=3",
            "--ignore-submodules=none", baseline, revision, "--",
        ).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("DIFF_NOT_UTF8") from None


# Root-level change logs the policy accepts (references/changelog-policy.md), any case.
_CHANGELOG_NAMES = frozenset({"changelog", "changelog.md", "changes.md", "history.md"})


_VERSION_LINE = re.compile(r"^(?:#+\s*)?\[?(\d+)\.(\d+)\.(\d+)\]?(?!:)(?:\s|$)")


def _top_version(text: str) -> tuple[int, int, int] | None:
    """The first version heading of a change log (`2.1.11 2026-...`, `0.1.1 - ...`,
    `## [0.1.1] - ...`); link-reference lines (`[0.1.1]: url`) are not headings."""
    for line in text.splitlines():
        found = _VERSION_LINE.match(line.strip())
        if found:
            return tuple(int(part) for part in found.groups())
    return None


def _changelog_version_error(root: Path, baseline: str, revision: str) -> str | None:
    """Every change bumps the change log's version: a new top version above the old one."""
    names = [line for line in _git(root, "diff", "--name-only", baseline, revision)
             .decode("utf-8", "replace").splitlines() if line]
    logs = [name for name in names if "/" not in name and name.lower() in _CHANGELOG_NAMES]
    if not logs:
        return None
    name = logs[0]
    new = _git(root, "show", f"{revision}:{name}").decode("utf-8", "replace")
    old = _git(root, "show", f"{baseline}:{name}", check=False).decode("utf-8", "replace")
    new_top, old_top = _top_version(new), _top_version(old)
    if new_top is None or (old_top is not None and new_top <= old_top):
        return "CHANGELOG_VERSION_NOT_BUMPED"
    return None


def _missing_changelog(root: Path, baseline: str, revision: str) -> bool:
    """True when the commit changes files but none of them is the repository's change log."""
    names = [line for line in _git(root, "diff", "--name-only", baseline, revision)
             .decode("utf-8", "replace").splitlines() if line]
    return bool(names) and not any(
        "/" not in name and name.lower() in _CHANGELOG_NAMES for name in names)


def _commits_above_baseline(root: Path, baseline: str) -> int:
    """How many commits HEAD carries beyond ``baseline`` (0 for a no-op worktree).

    iCode/Gerrit rejects a push with more than one commit per CR (HCK_F507), so this
    is the predicate the IMPLEMENT producer must keep at 0 or 1 before the change set's
    revisions bind.
    """
    out = _git(root, "rev-list", "--count", baseline + ".." + "HEAD").decode().strip()
    return int(out) if out.isdigit() else 0


def _ownership_rows(orchestrator: Any, run_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    import sqlite3
    from workspace_manager import _ownership_database

    database = _ownership_database(Path(orchestrator.workspaces.worktree_root))
    if not database.is_file():
        raise ValueError("WORKTREE_NOT_OWNED")
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM worktree_ownership WHERE run_id = ? AND status = 'ACTIVE'",
            (run_id,),
        ).fetchall()
    return {(row["task_id"], row["repo_path"]): dict(row) for row in rows}


def _owned_worktree(orchestrator: Any, rows: dict[tuple[str, str], dict[str, Any]],
                    run_id: str, task_id: str, repository: dict[str, Any]) -> Path:
    repo = Path(repository["path"]).expanduser().resolve()
    row = rows.get((task_id, str(repo)))
    if not row:
        raise ValueError("WORKTREE_NOT_OWNED")
    if row["baseline_revision"] != repository["revision"]:
        raise ValueError("WORKTREE_BASELINE_MISMATCH")
    checked = orchestrator.workspaces.query_ownership(
        repo, run_id, task_id, row["owner_token"]
    )
    if checked.get("status") != "VERIFIED" or checked.get("ownership_status") != "ACTIVE":
        raise ValueError(checked.get("reason_code", "WORKTREE_NOT_OWNED"))
    worktree = Path(row["worktree_path"]).resolve()
    if not worktree.is_dir():
        raise ValueError("WORKTREE_NOT_OWNED")
    return worktree


def prepare(orchestrator: Any, run_id: str, job_id: str,
            metadata: dict[str, Any]) -> dict[str, Any]:
    """Build content only; this function never writes state, artifacts, or commits."""
    if not isinstance(metadata, dict):
        return {"ok": False, "reason_code": "METADATA_INVALID"}
    job = orchestrator.state.producer_job(job_id)
    if not isinstance(job, dict) or job.get("run_id") != run_id:
        return {"ok": False, "reason_code": "PRODUCER_JOB_NOT_FOUND"}
    action = orchestrator.next(run_id, read_only=True)
    if not action.get("ok"):
        return action
    payload = job.get("payload") or {}
    if job_id != f"producer:{action.get('action_id')}" or any(
        key not in payload or payload[key] != action.get(key)
        for key in ("action_id", "phase", "task_id", "source_event_id",
                    "input_hash", "result_schema", "required_human_gate")
    ) or any(
        key in payload and payload[key] != action.get(key)
        for key in ("run_id", "parent_artifact_hash", "source_revisions",
                    "baseline_revisions", "reentry")
    ):
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB"}
    if action.get("phase") != "IMPLEMENT" or action.get("run_id") != run_id:
        return {"ok": False, "reason_code": "IMPLEMENT_ACTION_REQUIRED"}
    if job.get("status") == "FULFILLED":
        return {"ok": False, "reason_code": "OLD_G5_CANDIDATE_ISOLATED"}
    if job.get("status") != "PENDING":
        return {"ok": False, "reason_code": "PRODUCER_JOB_NOT_PENDING"}
    task_id = action.get("task_id")
    if not isinstance(task_id, str) or metadata.get("task_id") != task_id:
        return {"ok": False, "reason_code": "TASK_ID_MISMATCH"}
    allowed = {"change_set_id", "task_id", "test_ids", "traceability_delta", "deviations"}
    if set(metadata) != allowed or not isinstance(metadata.get("change_set_id"), str):
        return {"ok": False, "reason_code": "METADATA_INVALID"}

    protocol = orchestrator.phase_protocol()
    plan = protocol._predecessor(run_id, "PLAN", task_id)
    if not plan or not plan.get("valid"):
        return {"ok": False, "reason_code": "PLAN_PREDECESSOR_REQUIRED"}
    plan_content = plan["envelope"].get("content")
    if not isinstance(plan_content, dict) or plan_content.get("task_id") != task_id:
        return {"ok": False, "reason_code": "PLAN_PREDECESSOR_INVALID"}
    if plan["envelope"].get("content_hash") != action.get("parent_artifact_hash"):
        return {"ok": False, "reason_code": "PLAN_PREDECESSOR_MISMATCH"}
    baselines = {
        item.get("role"): item.get("revision")
        for item in plan_content.get("repositories", [])
        if isinstance(item, dict)
    }
    if (set(baselines) != {"business", "tests"}
            or baselines != action.get("baseline_revisions")
            or baselines != action.get("source_revisions")):
        return {"ok": False, "reason_code": "BASELINE_REVISION_MISMATCH"}
    expected_tests = {
        item.get("test_id") for item in plan_content.get("tests", [])
        if isinstance(item, dict)
    }
    if set(metadata.get("test_ids", [])) != expected_tests:
        return {"ok": False, "reason_code": "TEST_IDS_MISMATCH"}
    expected_points = set(plan_content.get("acceptance_point_ids", []))
    traces = metadata.get("traceability_delta")
    if not isinstance(traces, list) or {
        item.get("acceptance_point_id") for item in traces if isinstance(item, dict)
    } != expected_points:
        return {"ok": False, "reason_code": "TRACEABILITY_MISMATCH"}
    if any(not isinstance(item, dict) or not set(item.get("test_ids", []))
           or not set(item.get("test_ids", [])).issubset(expected_tests) for item in traces):
        return {"ok": False, "reason_code": "TRACEABILITY_TEST_IDS_MISMATCH"}

    profile_result = orchestrator._runtime_profile(run_id)
    if not profile_result.get("ok"):
        return {"ok": False, "reason_code": profile_result.get("reason_code", "PROJECT_NOT_READY")}
    profile = profile_result["profile"]
    try:
        rows = _ownership_rows(orchestrator, run_id)
        roots = {}
        repositories = plan_content["repositories"]
        for repo in repositories:
            allowed_repos = (profile.get("business_repos", []) if repo["role"] == "business"
                             else [profile.get("test_repo", {})])
            profile_repo = next(
                (
                    item for item in allowed_repos
                    if isinstance(item, dict)
                    and (
                        Path(item["path"]).expanduser().resolve()
                        == Path(repo["path"]).expanduser().resolve()
                        or any(
                            row.get("worktree_path")
                            and Path(row["worktree_path"]).expanduser().resolve()
                            == Path(repo["path"]).expanduser().resolve()
                            for (owned_task, owned_repo), row in rows.items()
                            if owned_task == task_id
                            and owned_repo == str(Path(item["path"]).expanduser().resolve())
                        )
                    )
                ),
                None,
            )
            if profile_repo is None:
                raise ValueError("PLAN_REPOSITORY_MISMATCH")
            selected_repo = dict(profile_repo)
            selected_repo["revision"] = repo["revision"]
            roots[repo["role"]] = _owned_worktree(
                orchestrator, rows, run_id, task_id, selected_repo)
        patches = {}
        revisions = {}
        for role, root in roots.items():
            _require_clean(root)
            revision = _head(root)
            import re
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baselines[role]):
                raise ValueError("BASELINE_REVISION_INVALID")
            if _git(root, "rev-parse", "--verify", baselines[role] + "^{commit}").decode().strip() != baselines[role]:
                raise ValueError("WORKTREE_BASELINE_MISMATCH")
            # iCode/Gerrit rejects a push that carries more than one commit per CR
            # (HCK_F507 "too_many_commits"). A resume, or a G5 "需修改" redo that adds
            # another commit, can leave >1 commit above the pinned baseline. Reject it
            # here -- before the revisions/candidate_hash bind and before G5/G7 -- so the
            # worktree is squashed to a single commit instead of the run only discovering
            # it at SUBMIT after both approvals were already spent.
            if _commits_above_baseline(root, baselines[role]) > 1:
                raise ValueError("MULTIPLE_COMMITS_ABOVE_BASELINE")
            # Every repository a change set touches records it in its own change log
            # (business and product-test alike), so a change can be traced per repo.
            if _missing_changelog(root, baselines[role], revision):
                raise ValueError(f"CHANGELOG_ENTRY_MISSING:{role}")
            version_error = _changelog_version_error(root, baselines[role], revision)
            if version_error:
                raise ValueError(f"{version_error}:{role}")
            # query_ownership verifies that HEAD descends from this baseline.
            patches[role] = _diff(root, baselines[role], revision)
            revisions[role] = revision
        for repo in repositories:
            root = roots[repo["role"]]
            profile_repo = next(
                (
                    item for item in (
                        profile.get("business_repos", [])
                        if repo["role"] == "business"
                        else [profile.get("test_repo", {})]
                    )
                    if isinstance(item, dict)
                    and (
                        Path(item["path"]).expanduser().resolve()
                        == Path(repo["path"]).expanduser().resolve()
                        or roots[repo["role"]].resolve()
                        == Path(repo["path"]).expanduser().resolve()
                    )
                ),
                None,
            )
            if profile_repo is None:
                raise ValueError("PLAN_REPOSITORY_MISMATCH")
            selected_repo = dict(profile_repo)
            selected_repo["revision"] = repo["revision"]
            _owned_worktree(orchestrator, rows, run_id, task_id, selected_repo)
            _require_clean(root)
            if _head(root) != revisions[repo["role"]]:
                raise ValueError("WORKTREE_HEAD_CHANGED")
    except (ValueError, OSError) as error:
        return {"ok": False, "reason_code": str(error)}

    content = {
        "change_set_id": metadata["change_set_id"],
        "task_id": task_id,
        "baseline_revisions": baselines,
        "revisions": revisions,
        "business_patch": patches["business"],
        "test_patch": patches["tests"],
        "patch_kinds": {role: "git-diff-v1" if patch else "no-op"
                        for role, patch in patches.items()},
        "full_diff_hash": _hash({
            "business_patch": patches["business"],
            "test_patch": patches["tests"],
        }),
        "test_ids": sorted(metadata["test_ids"]),
        "traceability_delta": metadata["traceability_delta"],
        "deviations": metadata["deviations"],
    }
    content["candidate_hash"] = _hash(content)
    checked = protocol.validate_content(action, content)
    if not checked.get("ok"):
        return checked
    if orchestrator.next(run_id, read_only=True) != action:
        return {"ok": False, "reason_code": "STALE_PRODUCER_JOB"}
    return {"ok": True, "reason_code": "CHANGE_SET_PREPARED", "content": content,
            "run_id": run_id, "job_id": job_id, "task_id": task_id}
