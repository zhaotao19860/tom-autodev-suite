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

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _git(root: Any, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_if_dirty(root: Any, message: str) -> str:
    """Commit with the repository's own identity.

    iCode refuses a push whose committer does not match the pushing account, so an
    invented identity such as a bot name makes every submission unpushable. The
    repository's configured user is the only identity that can be pushed, so a repo
    without one is a configuration error, not something to paper over.
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
    return _git(root, "rev-parse", "HEAD")


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
    business = next(
        (
            (repository, rows[(task_id, repository["path"])])
            for repository in profile.get("business_repos", [])
            if (task_id, repository["path"]) in rows
        ),
        None,
    )
    test_repository = profile.get("test_repo") or {}
    test_row = rows.get((task_id, test_repository.get("path")))
    if business is None or test_row is None:
        return {"ok": False, "reason_code": "WORKTREE_NOT_OWNED", "task_id": task_id}
    repository, business_row = business

    # The owner of a change set is the account that pushes it, which iCode then reports
    # as the CR's owner. Taking the alphabetically first role member instead named
    # whoever happened to sort first -- a test-role address, here -- so reconcile could
    # never recognise a CR this run had itself pushed.
    from clients.ku_client import resolve_username

    owner = resolve_username([business_row["worktree_path"], repository["path"]])
    if not owner:
        return {"ok": False, "reason_code": "OWNER_NOT_RESOLVED", "task_id": task_id}
    message = f"{_card_id(orchestrator, run_id)} {task_id} reviewed change set"
    business_revision = _commit_if_dirty(business_row["worktree_path"], message)
    test_revision = _commit_if_dirty(test_row["worktree_path"], message)

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
    revision_set_id = hashlib.sha256(_canonical(revision_set)).hexdigest()
    descriptor = {
        "run_id": run_id,
        "change_set_id": content["change_set_id"],
        "revision_set_id": revision_set_id,
        "repo_path": business_row["worktree_path"],
        "module": repository["module"],
        "target_branch": repository["branch"],
        "commit_revision": business_revision,
        "card_id": _card_id(orchestrator, run_id),
        "owner": owner,
        "revision_set": revision_set,
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
