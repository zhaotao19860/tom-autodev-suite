"""Turn a passed Review into the submit descriptor the iCode boundary asks for.

`clients/icode_runtime._validate_change_set()` binds a submission to a `change-set`
artifact whose content is exactly the canonical descriptor and whose metadata carries
`verdict: PASS`. Nothing produced that artifact, so SUBMIT could never start even with
three reviewed change sets in the ledger.

It is built the moment a Review passes, not later, and the worktrees are committed at
the same time. Committing here is what makes the reviewed bytes immutable: the
descriptor pins `commit_revision`, so a later edit to the worktree can no longer ride
into the submission behind a review that never saw it.
"""

from __future__ import annotations

from execution_guard import guard_execution

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _git(root: Any, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _matching_clean_worktree(preferred: str, revision: str) -> str | None:
    """Use the owned worktree, or a sibling owned checkout at the exact revision.

    A hardcoded machine path would bind every project to one BGW leftover. Sibling
    owned worktrees for the same repository identity are the only safe fallback:
    they already passed WorkspaceGate, and HEAD plus a clean tree are enough to
    prove they still hold the reviewed bytes.
    """
    preferred_path = Path(preferred)
    candidates = [preferred_path]
    run_root = preferred_path.parent.parent
    repo_identity = preferred_path.name
    if run_root.is_dir() and repo_identity:
        for sibling in sorted(run_root.glob(f"*/{repo_identity}")):
            if sibling.resolve() != preferred_path.resolve():
                candidates.append(sibling)
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if not candidate.is_dir():
            continue
        if not (candidate / ".git").exists():
            continue
        try:
            if _git(candidate, "rev-parse", "HEAD") != revision:
                continue
            if _git(candidate, "status", "--porcelain"):
                continue
        except (OSError, subprocess.CalledProcessError):
            continue
        return str(candidate)
    return None


def _commit_if_dirty(root: Any, message: str) -> str:
    """Commit with the repository's own identity, carrying a Change-Id.

    iCode refuses a push whose committer does not match the pushing account, so an
    invented identity such as a bot name makes every submission unpushable. The
    repository's configured user is the only identity that can be pushed, so a repo
    without one is a configuration error, not something to paper over.

    Gerrit also refuses a commit with no Change-Id trailer, and a worktree created by
    `git worktree add` does not inherit the primary checkout's commit-msg hook, so the
    hook cannot be relied on. The trailer is therefore written here, derived from the
    commit this call just produced: same commit, same Change-Id on a retry, and a new
    one for a genuinely new commit, which is exactly the identity Gerrit wants.
    """
    if _git(root, "status", "--porcelain"):
        name = _git(root, "config", "--get", "user.name")
        email = _git(root, "config", "--get", "user.email")
        if not name or not email:
            raise ValueError("GIT_IDENTITY_REQUIRED")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", message],
            check=True,
        )
        if "Change-Id:" not in _git(root, "log", "-1", "--format=%B"):
            change_id = "I" + hashlib.sha1(
                _git(root, "rev-parse", "HEAD").encode("utf-8")
            ).hexdigest()
            amended = f"{_git(root, 'log', '-1', '--format=%B').rstrip()}\n\nChange-Id: {change_id}\n"
            subprocess.run(
                ["git", "-C", str(root), "commit", "-q", "--amend", "-m", amended],
                check=True,
            )
    return _require_pushable_head(root)


def _require_pushable_head(root: Any) -> str:
    """Refuse a HEAD iCode cannot push, even when the worktree is already clean.

    IMPLEMENT can commit before Review. If that commit used a bot identity or omitted
    the Change-Id trailer, `_commit_if_dirty` used to return it unchanged and the
    later `push_cr` failed as SUBMIT_REJECTED with no local diagnosis.
    """
    revision = _git(root, "rev-parse", "HEAD")
    body = _git(root, "log", "-1", "--format=%B")
    if "Change-Id:" not in body:
        raise ValueError("CHANGE_ID_MISSING")
    email = _git(root, "log", "-1", "--format=%ce")
    configured = _git(root, "config", "--get", "user.email")
    if not configured:
        raise ValueError("GIT_IDENTITY_REQUIRED")
    if email.split("@", 1)[0].strip() != configured.split("@", 1)[0].strip():
        raise ValueError("GIT_IDENTITY_MISMATCH")
    return revision


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _ownership_rows(orchestrator: Any, run_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    import sqlite3

    import workspace_manager

    database = workspace_manager._ownership_database(Path(orchestrator.workspaces.worktree_root))
    if not database.exists():
        return {}
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {
            (row["task_id"], row["repo_path"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM worktree_ownership WHERE run_id = ? AND status = 'ACTIVE'",
                (run_id,),
            )
        }
    finally:
        connection.close()


def owned_row(
    rows: dict[tuple[str, str], dict[str, Any]], task_id: str, path: Any
) -> dict[str, Any] | None:
    """Look up an ACTIVE worktree-ownership row for (task_id, repo path).

    The ownership map is keyed by the fully resolved repo path (`_ownership_rows`
    reflects `workspace_manager`, which always stores
    `str(Path(...).expanduser().resolve())`), so every reader must resolve the profile
    path the same way before the lookup. Doing it here — in one shared helper both the
    descriptor build and `orchestrator._submit` call — keeps the canonical form defined
    once; a symlinked repo root (e.g. macOS `/var` -> `/private/var`), a `~`-relative
    path, or a trailing slash otherwise misses every row and reports WORKTREE_NOT_OWNED.
    """
    if not isinstance(path, str) or not path:
        return None
    return rows.get((task_id, str(Path(path).expanduser().resolve())))


@guard_execution
def build_and_archive(orchestrator: Any, run_id: str, task_id: str) -> dict[str, Any]:
    """Archive the descriptor for one task, or say precisely why it cannot be built."""
    try:
        return _build_and_archive(orchestrator, run_id, task_id)
    except Exception as error:  # noqa: BLE001 - a descriptor gap must not fail the phase
        return {"ok": False, "reason_code": "SUBMIT_DESCRIPTOR_FAILED", "detail": str(error)}


def _build_and_archive(orchestrator: Any, run_id: str, task_id: str) -> dict[str, Any]:
    from phase_protocol import _passing_review

    review = orchestrator.artifacts.latest_phase(run_id, "REVIEW", task_id)
    if not review.get("valid") or not _passing_review(review["envelope"]["content"]):
        return {"ok": False, "reason_code": "REVIEW_NOT_PASSING", "task_id": task_id}
    change_set = orchestrator.artifacts.latest_phase(run_id, "IMPLEMENT", task_id)
    if not change_set.get("valid"):
        return {"ok": False, "reason_code": "CHANGE_SET_MISSING", "task_id": task_id}
    content = change_set["envelope"]["content"]
    if review["envelope"]["content"].get("change_set_hash") != content.get("candidate_hash"):
        return {"ok": False, "reason_code": "REVIEW_CHANGE_SET_MISMATCH", "task_id": task_id}

    pinned = orchestrator._runtime_profile(run_id)
    if not pinned.get("ok"):
        return {"ok": False, "reason_code": pinned.get("reason_code", "PROJECT_NOT_READY")}
    profile = pinned["profile"]
    rows = _ownership_rows(orchestrator, run_id)
    # Ownership rows are keyed by the fully resolved repo path; `owned_row` resolves the
    # profile path the same way workspace_manager stored it (see its docstring).
    business = next(
        (
            (repository, owned_row(rows, task_id, repository["path"]))
            for repository in profile.get("business_repos", [])
            if owned_row(rows, task_id, repository["path"]) is not None
        ),
        None,
    )
    test_repository = profile.get("test_repo") or {}
    test_row = owned_row(rows, task_id, test_repository.get("path"))
    if business is None or test_row is None:
        return {"ok": False, "reason_code": "WORKTREE_NOT_OWNED", "task_id": task_id}
    repository, business_row = business
    expected_revisions = content.get("revisions")
    if isinstance(expected_revisions, dict):
        replacement = _matching_clean_worktree(
            test_row["worktree_path"], expected_revisions.get("tests", "")
        )
        if replacement is not None and replacement != test_row["worktree_path"]:
            test_row = {**test_row, "worktree_path": replacement}

    message = f"{_card_id(orchestrator, run_id)} {task_id} reviewed change set"
    try:
        business_revision = _commit_if_dirty(business_row["worktree_path"], message)
        test_revision = _commit_if_dirty(test_row["worktree_path"], message)
    except ValueError as error:
        return {"ok": False, "reason_code": str(error), "task_id": task_id}
    expected_revisions = content.get("revisions")
    if (
        not isinstance(expected_revisions, dict)
        or business_revision != expected_revisions.get("business")
        or test_revision != expected_revisions.get("tests")
    ):
        return {
            "ok": False,
            "reason_code": "DESCRIPTOR_REVISION_MISMATCH",
            "task_id": task_id,
            "expected_revisions": expected_revisions,
            "actual_revisions": {
                "business": business_revision,
                "tests": test_revision,
            },
        }

    revision_set = {
        "business": {
            "module": repository["module"],
            "revision": business_revision,
            "branch": repository["branch"],
        },
        "test": {
            "module": test_repository["module"],
            "revision": test_revision,
            "branch": test_repository["branch"],
        },
    }

    # The primary submission is the repository that actually has a new commit. A task can
    # legitimately touch only the test repository -- a Spec amendment that retires stale
    # product cases is exactly that shape -- and pinning the primary submission to the
    # business repository regardless produced a descriptor whose commit_revision equalled
    # the baseline, so the push carried no diff and iCode refused it.
    primary_role = "business"
    if business_revision == business_row["baseline_revision"] and (
        test_revision != test_row["baseline_revision"]
    ):
        primary_role = "test"
    primary_repository = repository if primary_role == "business" else test_repository
    primary_row = business_row if primary_role == "business" else test_row
    primary_revision = business_revision if primary_role == "business" else test_revision

    # The owner of a change set is the account that pushes it, which iCode then reports
    # as the CR's owner. Taking the alphabetically first role member instead named
    # whoever happened to sort first -- a test-role address, here -- so reconcile could
    # never recognise a CR this run had itself pushed.
    from clients.ku_client import resolve_username

    owner = resolve_username([primary_row["worktree_path"], primary_repository["path"]])
    if not owner:
        return {"ok": False, "reason_code": "OWNER_NOT_RESOLVED", "task_id": task_id}

    revision_set_id = hashlib.sha256(_canonical(revision_set)).hexdigest()
    descriptor = {
        "run_id": run_id,
        "change_set_id": content["change_set_id"],
        "revision_set_id": revision_set_id,
        "repo_path": primary_row["worktree_path"],
        "module": primary_repository["module"],
        "target_branch": primary_repository["branch"],
        "commit_revision": primary_revision,
        "card_id": _card_id(orchestrator, run_id),
        "owner": owner,
        "revision_set": revision_set,
        "submission_mode": "create_new_cr",
    }
    canonical_bytes = _canonical(descriptor)
    archived = orchestrator.artifacts.put(
        run_id,
        "change-set",
        canonical_bytes,
        {
            "verdict": "PASS",
            "revision_set_id": revision_set_id,
            "task_id": task_id,
            "reviewed_artifact_id": review["artifact_id"],
            "change_set_hash": content["candidate_hash"],
        },
    )
    return {
        "ok": True,
        "reason_code": "OK",
        "task_id": task_id,
        "descriptor": {
            **descriptor,
            "input_hash": hashlib.sha256(canonical_bytes).hexdigest(),
            "reviewed_artifact_id": archived["artifact_id"],
        },
    }


def _card_id(orchestrator: Any, run_id: str) -> str:
    events = orchestrator.state.events(run_id)
    payload = events[0].get("payload") if events else None
    snapshot = payload.get("requirement_snapshot") if isinstance(payload, dict) else None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("canonical_card_id"), str):
        return snapshot["canonical_card_id"]
    return ""
