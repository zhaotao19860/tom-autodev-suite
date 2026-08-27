from __future__ import annotations

import fcntl
import hashlib
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkspaceManager:
    def __init__(self, worktree_root: Path | str | None = None):
        self.worktree_root = (
            Path(worktree_root).expanduser().resolve() if worktree_root is not None else None
        )

    def inspect(
        self,
        repo_path: Path | str,
        run_id: str,
        task_id: str,
        *,
        baseline_evidence: dict[str, Any] | None = None,
        environment_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        requested_repo = Path(repo_path).expanduser().resolve()
        top_level = _git(requested_repo, "rev-parse", "--show-toplevel")
        if top_level is None:
            return {
                "run_id": run_id,
                "task_id": task_id,
                "repo_path": str(requested_repo),
                "worktree_path": None,
                "baseline_revision": None,
                "branch": None,
                "user_changes": [],
                "lock_key": f"{requested_repo}:unknown",
                "baseline_status": "WORKSPACE_INVALID",
            }

        repo = Path(top_level).resolve()
        revision = _git(repo, "rev-parse", "HEAD")
        branch = _git(repo, "branch", "--show-current")
        user_changes = _status_paths(repo)
        status = "BASELINE_UNVERIFIED"
        if baseline_evidence is not None:
            if baseline_evidence.get("revision") != revision:
                status = "REVISION_MISMATCH"
            elif (
                environment_fingerprint is not None
                and baseline_evidence.get("environment_fingerprint") != environment_fingerprint
            ):
                status = "ENV_FINGERPRINT_MISMATCH"
            else:
                status = "READY"

        return {
            "run_id": run_id,
            "task_id": task_id,
            "repo_path": str(repo),
            "worktree_path": None,
            "baseline_revision": revision,
            "branch": branch,
            "user_changes": user_changes,
            "lock_key": f"{repo}:{task_id}",
            "baseline_status": status,
        }

    def query_ownership(
        self,
        repo_path: Path | str,
        run_id: str,
        task_id: str,
        owner_token: str,
    ) -> dict[str, Any]:
        """Verify durable worktree ownership without recovery or cleanup."""
        requested_repo = Path(repo_path).expanduser().resolve()
        top_level = _git(requested_repo, "rev-parse", "--show-toplevel")
        if top_level is None:
            return {"status": "BLOCKED", "reason_code": "WORKSPACE_INVALID"}
        repo = Path(top_level).resolve()
        run_component = _component(run_id)
        task_component = _component(task_id)
        if run_component is None or task_component is None:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_COMPONENT_INVALID"}
        root = (self.worktree_root or repo.parent / f".{repo.name}-tom-autodev-worktrees").resolve()
        if _same_or_within(root, repo):
            return {"status": "BLOCKED", "reason_code": "WORKTREE_ROOT_INVALID"}
        repo_identity = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
        worktree = root / run_component / task_component / repo_identity
        database = _ownership_database(root)
        if not database.is_file():
            return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_OWNED"}
        try:
            uri = f"{database.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as connection:
                connection.row_factory = sqlite3.Row
                ownership = connection.execute(
                    "SELECT * FROM worktree_ownership WHERE worktree_path = ?",
                    (str(worktree),),
                ).fetchone()
        except sqlite3.Error:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_OWNERSHIP_QUERY_FAILED"}
        if ownership is None or ownership["repo_path"] != str(repo):
            return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_OWNED"}
        if ownership["run_id"] != run_id or ownership["task_id"] != task_id:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_RECEIPT_MISMATCH"}
        if ownership["owner_token"] != owner_token:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_OWNER_MISMATCH"}
        if ownership["status"] != "ACTIVE":
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_NOT_ACTIVE",
                ownership["status"],
                "STOP",
            )
        expected_revision = ownership["baseline_revision"]
        if not expected_revision:
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_BASELINE_UNKNOWN",
                "ACTIVE",
                "STOP",
            )
        registered_worktrees = _registered_worktrees(repo)
        if registered_worktrees is None:
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_REGISTRATION_QUERY_FAILED",
                "ACTIVE",
                "STOP",
            )
        if worktree not in registered_worktrees or not worktree.exists():
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_ACTIVE_MISMATCH",
                "ACTIVE",
                "STOP",
            )
        mismatch = _registered_worktree_mismatch(repo, worktree, expected_revision)
        if mismatch is not None:
            return _reconcile_result(
                ownership,
                "BLOCKED",
                mismatch,
                "ACTIVE",
                "STOP",
            )
        return _reconcile_result(ownership, "VERIFIED", None, "ACTIVE", "NONE")

    def create(
        self,
        repo_path: Path | str,
        run_id: str,
        task_id: str,
        baseline: dict[str, Any],
        *,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        requested_repo = Path(repo_path).expanduser().resolve()
        top_level = _git(requested_repo, "rev-parse", "--show-toplevel")
        if top_level is None:
            return _create_failure(requested_repo, run_id, task_id, "WORKSPACE_INVALID")
        repo = Path(top_level).resolve()
        if baseline.get("baseline_status") != "READY":
            return _create_failure(repo, run_id, task_id, "BASELINE_UNVERIFIED")
        revision = baseline.get("baseline_revision")
        if not isinstance(revision, str) or not revision:
            return _create_failure(repo, run_id, task_id, "BASELINE_UNVERIFIED")

        current_revision = _git(repo, "rev-parse", "HEAD")
        verified_revision = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
        if current_revision != revision or verified_revision != revision:
            return _create_failure(repo, run_id, task_id, "REVISION_MISMATCH")

        run_component = _component(run_id)
        task_component = _component(task_id)
        if run_component is None or task_component is None:
            return _create_failure(repo, run_id, task_id, "WORKTREE_COMPONENT_INVALID")
        root = (self.worktree_root or repo.parent / f".{repo.name}-tom-autodev-worktrees").resolve()
        if _same_or_within(root, repo):
            return _create_failure(repo, run_id, task_id, "WORKTREE_ROOT_INVALID")
        repo_identity = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
        worktree = root / run_component / task_component / repo_identity
        if not _within(worktree, root):
            return _create_failure(repo, run_id, task_id, "WORKTREE_PATH_CONFLICT")
        if worktree.resolve() == repo:
            return _create_failure(repo, run_id, task_id, "WORKTREE_PATH_CONFLICT")

        user_changes = _status_paths(repo)
        branch = _git(repo, "branch", "--show-current")
        with _ownership_operation_lock(root):
            return self._create_with_ownership_lock(
                root,
                repo,
                worktree,
                run_id,
                task_id,
                revision,
                branch,
                user_changes,
                owner_token,
            )

    def _create_with_ownership_lock(
        self,
        root: Path,
        repo: Path,
        worktree: Path,
        run_id: str,
        task_id: str,
        revision: str,
        branch: str | None,
        user_changes: list[str],
        requested_owner_token: str | None,
    ) -> dict[str, Any]:
        reservation = self._reserve(
            root,
            repo,
            worktree,
            run_id,
            task_id,
            revision,
            requested_owner_token,
        )
        if reservation["reason_code"] is not None:
            return _create_failure(repo, run_id, task_id, reservation["reason_code"])
        owner_token = reservation["owner_token"]
        if reservation["existing"]:
            recovered = self.reconcile(
                repo, run_id, task_id, owner_token, _lock_held=True
            )
            if recovered["ownership_status"] == "ACTIVE":
                return _created(
                    repo,
                    worktree,
                    run_id,
                    task_id,
                    revision,
                    branch,
                    user_changes,
                    owner_token,
                )
            retryable_tombstone = (
                recovered["ownership_status"] == "REMOVED"
                and recovered["status"] == "ALREADY_ABSENT"
            )
            if recovered.get("action") != "RETRY_CREATE" and not retryable_tombstone:
                return _create_failure(
                    repo,
                    run_id,
                    task_id,
                    recovered["reason_code"] or "WORKTREE_RECOVERY_REQUIRED",
                    owner_token,
                )
            ownership_status = recovered["ownership_status"]
            if ownership_status != "RESERVED" and not self._change_ownership_status(
                root,
                repo,
                worktree,
                owner_token,
                ownership_status,
                "RESERVED",
            ):
                return _create_failure(
                    repo, run_id, task_id, "WORKTREE_RECOVERY_CONFLICT", owner_token
                )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        result = _git_result(repo, "worktree", "add", "--detach", str(worktree), revision)
        if result is None:
            cleanup_reason = self._cleanup_partial(repo, root, worktree, owner_token)
            return _create_failure(
                repo,
                run_id,
                task_id,
                cleanup_reason or "WORKTREE_CREATE_FAILED",
                owner_token,
            )
        if _git(repo, "rev-parse", "HEAD") != revision:
            cleanup_reason = self._cleanup_partial(repo, root, worktree, owner_token)
            return _create_failure(
                repo,
                run_id,
                task_id,
                cleanup_reason or "REVISION_MISMATCH",
                owner_token,
            )
        if _git(worktree, "rev-parse", "HEAD") != revision:
            cleanup_reason = self._cleanup_partial(repo, root, worktree, owner_token)
            return _create_failure(
                repo,
                run_id,
                task_id,
                cleanup_reason or "WORKTREE_REVISION_MISMATCH",
                owner_token,
            )
        if not self._change_ownership_status(
            root, repo, worktree, owner_token, "RESERVED", "ACTIVE"
        ):
            return _create_failure(
                repo, run_id, task_id, "WORKTREE_OWNER_MISMATCH", owner_token
            )
        return _created(
            repo,
            worktree,
            run_id,
            task_id,
            revision,
            branch,
            user_changes,
            owner_token,
        )

    def remove(
        self,
        repo_path: Path | str,
        worktree_path: Path | str,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        requested_repo = Path(repo_path).expanduser().resolve()
        top_level = _git(requested_repo, "rev-parse", "--show-toplevel")
        if top_level is None:
            return {"status": "BLOCKED", "reason_code": "WORKSPACE_INVALID"}
        repo = Path(top_level).resolve()
        root = (self.worktree_root or repo.parent / f".{repo.name}-tom-autodev-worktrees").resolve()
        worktree = Path(worktree_path).expanduser().resolve()
        if _same_or_within(root, repo) or not _within(worktree, root) or worktree == repo:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_PATH_CONFLICT"}
        if not owner_token:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_OWNER_REQUIRED"}

        with _ownership_operation_lock(root):
            ownership = self._ownership(root, worktree)
            if ownership is None or ownership["repo_path"] != str(repo):
                return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_OWNED"}
            if ownership["owner_token"] != owner_token:
                return {"status": "BLOCKED", "reason_code": "WORKTREE_OWNER_MISMATCH"}
            if ownership["status"] in {"REMOVED", "REMOVING"}:
                return self.reconcile(
                    repo,
                    ownership["run_id"],
                    ownership["task_id"],
                    owner_token,
                    _lock_held=True,
                )
            if ownership["status"] != "ACTIVE":
                return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_ACTIVE"}
            expected_revision = ownership["baseline_revision"]
            if not expected_revision:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_BASELINE_UNKNOWN",
                    "ACTIVE",
                    "STOP",
                )
            registered_worktrees = _registered_worktrees(repo)
            if registered_worktrees is None:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_REGISTRATION_QUERY_FAILED",
                    "ACTIVE",
                    "RETRY_RECONCILE",
                )
            if worktree not in registered_worktrees or not worktree.exists():
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_ACTIVE_MISMATCH",
                    "ACTIVE",
                    "STOP",
                )
            mismatch = _registered_worktree_mismatch(
                repo, worktree, expected_revision
            )
            if mismatch is not None:
                return _reconcile_result(
                    ownership, "BLOCKED", mismatch, "ACTIVE", "STOP"
                )
            if not self._change_ownership_status(
                root, repo, worktree, owner_token, "ACTIVE", "REMOVING"
            ):
                return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_ACTIVE"}
            return self.reconcile(
                repo,
                ownership["run_id"],
                ownership["task_id"],
                owner_token,
                _lock_held=True,
            )

    def reconcile(
        self,
        repo_path: Path | str,
        run_id: str,
        task_id: str,
        owner_token: str,
        *,
        _lock_held: bool = False,
    ) -> dict[str, Any]:
        requested_repo = Path(repo_path).expanduser().resolve()
        top_level = _git(requested_repo, "rev-parse", "--show-toplevel")
        if top_level is None:
            return {"status": "BLOCKED", "reason_code": "WORKSPACE_INVALID"}
        repo = Path(top_level).resolve()
        run_component = _component(run_id)
        task_component = _component(task_id)
        if run_component is None or task_component is None:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_COMPONENT_INVALID"}
        root = (self.worktree_root or repo.parent / f".{repo.name}-tom-autodev-worktrees").resolve()
        if _same_or_within(root, repo):
            return {"status": "BLOCKED", "reason_code": "WORKTREE_ROOT_INVALID"}
        repo_identity = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
        worktree = root / run_component / task_component / repo_identity
        if not _lock_held:
            with _ownership_operation_lock(root):
                return self.reconcile(
                    repo, run_id, task_id, owner_token, _lock_held=True
                )
        ownership = self._ownership(root, worktree)
        if ownership is None or ownership["repo_path"] != str(repo):
            return {"status": "BLOCKED", "reason_code": "WORKTREE_NOT_OWNED"}
        if ownership["run_id"] != run_id or ownership["task_id"] != task_id:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_RECEIPT_MISMATCH"}
        if ownership["owner_token"] != owner_token:
            return {"status": "BLOCKED", "reason_code": "WORKTREE_OWNER_MISMATCH"}

        expected_revision = ownership["baseline_revision"]
        registered_worktrees = _registered_worktrees(repo)
        if registered_worktrees is None:
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_REGISTRATION_QUERY_FAILED",
                ownership["status"],
                "RETRY_RECONCILE",
            )
        registered = worktree in registered_worktrees
        path_exists = worktree.exists()
        status = ownership["status"]

        if status in {"ACTIVE", "RESERVED"} and not expected_revision:
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_BASELINE_UNKNOWN",
                status,
                "STOP",
            )

        if status == "ACTIVE":
            mismatch = (
                _registered_worktree_mismatch(repo, worktree, expected_revision)
                if registered and path_exists
                else "WORKTREE_ACTIVE_MISMATCH"
            )
            if mismatch is None:
                return _reconcile_result(
                    ownership, "RECONCILED", None, "ACTIVE", "NONE"
                )
            return _reconcile_result(
                ownership,
                "BLOCKED",
                "WORKTREE_ACTIVE_MISMATCH",
                "ACTIVE",
                "STOP",
            )

        if status == "RESERVED":
            if registered:
                if _git(repo, "rev-parse", "HEAD") != expected_revision:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "REVISION_MISMATCH",
                        "RESERVED",
                        "STOP",
                    )
                mismatch = (
                    _registered_worktree_mismatch(repo, worktree, expected_revision)
                    if path_exists
                    else "WORKTREE_REVISION_MISMATCH"
                )
                if mismatch is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        mismatch,
                        "RESERVED",
                        "STOP",
                    )
                if not self._change_ownership_status(
                    root, repo, worktree, owner_token, "RESERVED", "ACTIVE"
                ):
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "WORKTREE_RECOVERY_CONFLICT",
                        "RESERVED",
                        "STOP",
                    )
                ownership = self._ownership(root, worktree)
                return _reconcile_result(
                    ownership, "RECONCILED", None, "ACTIVE", "NONE"
                )
            if path_exists:
                blocker = _unregistered_checkout_blocker(worktree)
                if blocker is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        blocker,
                        "RESERVED",
                        "STOP",
                    )
                shutil.rmtree(worktree, ignore_errors=True)
                if worktree.exists():
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "WORKTREE_CLEANUP_FAILED",
                        "RESERVED",
                        "STOP",
                    )
                _remove_empty_parents(worktree.parent, root)
            return _reconcile_result(
                ownership,
                "RECOVERY_REQUIRED",
                None,
                "RESERVED",
                "RETRY_CREATE",
            )

        if status in {"FAILED", "CLEANING"}:
            if registered:
                mismatch = _registered_worktree_mismatch(
                    repo, worktree, expected_revision
                )
                if mismatch is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        mismatch,
                        status,
                        "STOP",
                    )
                if _git_result(repo, "worktree", "remove", "--force", str(worktree)) is None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "WORKTREE_CLEANUP_FAILED",
                        status,
                        "STOP",
                    )
            elif worktree.exists():
                blocker = _unregistered_checkout_blocker(worktree)
                if blocker is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        blocker,
                        status,
                        "STOP",
                    )
                shutil.rmtree(worktree, ignore_errors=True)
            registered_worktrees = _registered_worktrees(repo)
            if registered_worktrees is None:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_REGISTRATION_QUERY_FAILED",
                    status,
                    "RETRY_RECONCILE",
                )
            if worktree.exists() or worktree in registered_worktrees:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_CLEANUP_FAILED",
                    status,
                    "STOP",
                )
            _remove_empty_parents(worktree.parent, root)
            if status == "CLEANING":
                if not self._change_ownership_status(
                    root, repo, worktree, owner_token, "CLEANING", "FAILED"
                ):
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "WORKTREE_RECOVERY_CONFLICT",
                        "CLEANING",
                        "STOP",
                    )
                ownership = self._ownership(root, worktree)
            return _reconcile_result(
                ownership, "RECOVERY_REQUIRED", None, "FAILED", "RETRY_CREATE"
            )

        if status == "REMOVING":
            had_worktree = registered or path_exists
            if registered:
                mismatch = _registered_worktree_mismatch(
                    repo, worktree, expected_revision
                )
                if mismatch is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        mismatch,
                        "REMOVING",
                        "STOP",
                    )
                if _git_result(
                    repo, "worktree", "remove", "--force", str(worktree)
                ) is None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        "WORKTREE_REMOVE_FAILED",
                        "REMOVING",
                        "RETRY_REMOVE",
                    )
            elif worktree.exists():
                blocker = _unregistered_checkout_blocker(worktree)
                if blocker is not None:
                    return _reconcile_result(
                        ownership,
                        "BLOCKED",
                        blocker,
                        "REMOVING",
                        "STOP",
                    )
                shutil.rmtree(worktree, ignore_errors=True)
            registered_worktrees = _registered_worktrees(repo)
            if registered_worktrees is None:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_REGISTRATION_QUERY_FAILED",
                    "REMOVING",
                    "RETRY_RECONCILE",
                )
            if worktree.exists() or worktree in registered_worktrees:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_REMOVE_FAILED",
                    "REMOVING",
                    "RETRY_REMOVE",
                )
            _remove_empty_parents(worktree.parent, root)
            if not self._change_ownership_status(
                root, repo, worktree, owner_token, "REMOVING", "REMOVED"
            ):
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_RECOVERY_CONFLICT",
                    "REMOVING",
                    "STOP",
                )
            ownership = self._ownership(root, worktree)
            return _reconcile_result(
                ownership,
                "REMOVED" if had_worktree else "ALREADY_ABSENT",
                None,
                "REMOVED",
                "NONE",
            )

        if status == "REMOVED":
            if registered or path_exists:
                return _reconcile_result(
                    ownership,
                    "BLOCKED",
                    "WORKTREE_REMOVED_PATH_PRESENT",
                    "REMOVED",
                    "STOP",
                )
            return _reconcile_result(
                ownership, "ALREADY_ABSENT", None, "REMOVED", "NONE"
            )

        return _reconcile_result(
            ownership, "BLOCKED", "WORKTREE_STATUS_INVALID", status, "STOP"
        )

    def _cleanup_partial(
        self, repo: Path, root: Path, worktree: Path, owner_token: str
    ) -> str | None:
        if not _within(worktree, root) or worktree == repo:
            return "WORKTREE_PATH_CONFLICT"
        ownership = self._ownership(root, worktree)
        if ownership is None or ownership["repo_path"] != str(repo):
            return "WORKTREE_NOT_OWNED"
        if ownership["owner_token"] != owner_token:
            return "WORKTREE_OWNER_MISMATCH"
        expected_revision = ownership["baseline_revision"]
        registered_worktrees = _registered_worktrees(repo)
        if registered_worktrees is None:
            return "WORKTREE_REGISTRATION_QUERY_FAILED"
        registered = worktree in registered_worktrees
        if registered:
            mismatch = _registered_worktree_mismatch(
                repo, worktree, expected_revision
            )
            if mismatch is not None:
                return mismatch
        elif worktree.exists():
            blocker = _unregistered_checkout_blocker(worktree)
            if blocker is not None:
                return blocker
        if not self._change_ownership_status(
            root, repo, worktree, owner_token, "RESERVED", "CLEANING"
        ):
            return "WORKTREE_RECOVERY_CONFLICT"
        if registered:
            mismatch = _registered_worktree_mismatch(
                repo, worktree, expected_revision
            )
            if mismatch is not None:
                return mismatch
            _git_result(repo, "worktree", "remove", "--force", str(worktree))
        elif worktree.exists():
            blocker = _unregistered_checkout_blocker(worktree)
            if blocker is not None:
                return blocker
            shutil.rmtree(worktree, ignore_errors=True)
        registered_worktrees = _registered_worktrees(repo)
        if registered_worktrees is None:
            return "WORKTREE_REGISTRATION_QUERY_FAILED"
        if worktree.exists() or worktree in registered_worktrees:
            return "WORKTREE_CLEANUP_FAILED"
        _remove_empty_parents(worktree.parent, root)
        if not self._change_ownership_status(
            root, repo, worktree, owner_token, "CLEANING", "FAILED"
        ):
            return "WORKTREE_RECOVERY_CONFLICT"
        return None

    def _reserve(
        self,
        root: Path,
        repo: Path,
        worktree: Path,
        run_id: str,
        task_id: str,
        baseline_revision: str,
        requested_owner_token: str | None,
    ) -> dict[str, Any]:
        owner_token = uuid.uuid4().hex
        with self._ownership_connection(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM worktree_ownership WHERE worktree_path = ?",
                (str(worktree),),
            ).fetchone()
            if existing is not None:
                if requested_owner_token is None:
                    return {
                        "owner_token": None,
                        "reason_code": "WORKTREE_ALREADY_RESERVED",
                        "existing": True,
                    }
                if existing["owner_token"] != requested_owner_token:
                    return {
                        "owner_token": None,
                        "reason_code": "WORKTREE_OWNER_MISMATCH",
                        "existing": True,
                    }
                if (
                    existing["repo_path"] != str(repo)
                    or existing["run_id"] != run_id
                    or existing["task_id"] != task_id
                    or (
                        existing["baseline_revision"] is not None
                        and existing["baseline_revision"] != baseline_revision
                    )
                ):
                    return {
                        "owner_token": None,
                        "reason_code": "WORKTREE_RECEIPT_MISMATCH",
                        "existing": True,
                    }
                if existing["baseline_revision"] is None:
                    connection.execute(
                        """
                        UPDATE worktree_ownership
                        SET baseline_revision = ?, updated_at = ?
                        WHERE worktree_path = ? AND owner_token = ?
                          AND baseline_revision IS NULL
                        """,
                        (
                            baseline_revision,
                            _now(),
                            str(worktree),
                            requested_owner_token,
                        ),
                    )
                return {
                    "owner_token": requested_owner_token,
                    "reason_code": None,
                    "existing": True,
                }
            if requested_owner_token is not None:
                return {
                    "owner_token": None,
                    "reason_code": "WORKTREE_NOT_OWNED",
                    "existing": False,
                }
            registered_worktrees = _registered_worktrees(repo)
            if registered_worktrees is None:
                return {
                    "owner_token": None,
                    "reason_code": "WORKTREE_REGISTRATION_QUERY_FAILED",
                    "existing": False,
                }
            if worktree.exists() or worktree in registered_worktrees:
                return {
                    "owner_token": None,
                    "reason_code": "WORKTREE_PATH_CONFLICT",
                    "existing": False,
                }
            now = _now()
            connection.execute(
                """
                INSERT INTO worktree_ownership(
                    worktree_path, repo_path, run_id, task_id, owner_token,
                    baseline_revision, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
                """,
                (
                    str(worktree),
                    str(repo),
                    run_id,
                    task_id,
                    owner_token,
                    baseline_revision,
                    now,
                    now,
                ),
            )
            self._record_ownership_transition(
                connection, worktree, owner_token, None, "RESERVED", now
            )
        return {"owner_token": owner_token, "reason_code": None, "existing": False}

    def _ownership(self, root: Path, worktree: Path) -> sqlite3.Row | None:
        with self._ownership_connection(root) as connection:
            return connection.execute(
                "SELECT * FROM worktree_ownership WHERE worktree_path = ?",
                (str(worktree),),
            ).fetchone()

    def _change_ownership_status(
        self,
        root: Path,
        repo: Path,
        worktree: Path,
        owner_token: str,
        expected_status: str,
        new_status: str,
    ) -> bool:
        with self._ownership_connection(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            updated = connection.execute(
                """
                UPDATE worktree_ownership
                SET status = ?, updated_at = ?
                WHERE worktree_path = ? AND repo_path = ? AND owner_token = ?
                  AND status = ?
                """,
                (
                    new_status,
                    now,
                    str(worktree),
                    str(repo),
                    owner_token,
                    expected_status,
                ),
            )
            if updated.rowcount == 1:
                self._record_ownership_transition(
                    connection,
                    worktree,
                    owner_token,
                    expected_status,
                    new_status,
                    now,
                )
            return updated.rowcount == 1

    def _record_ownership_transition(
        self,
        connection: sqlite3.Connection,
        worktree: Path,
        owner_token: str,
        previous_status: str | None,
        status: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO worktree_ownership_history(
                worktree_path, owner_token, previous_status, status, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (str(worktree), owner_token, previous_status, status, created_at),
        )

    def _ownership_connection(self, root: Path) -> sqlite3.Connection:
        database = _ownership_database(root)
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_ownership (
                worktree_path TEXT PRIMARY KEY,
                repo_path TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                owner_token TEXT NOT NULL UNIQUE,
                baseline_revision TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(worktree_ownership)").fetchall()
        }
        if "baseline_revision" not in columns:
            connection.execute(
                "ALTER TABLE worktree_ownership ADD COLUMN baseline_revision TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worktree_ownership_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                worktree_path TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                previous_status TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        return connection


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def _status_paths(repo: Path) -> list[str]:
    status = _git(repo, "status", "--porcelain") or ""
    return [line[3:] for line in status.splitlines() if len(line) >= 4]


def _component(value: str) -> str | None:
    if (
        not isinstance(value, str)
        or not _COMPONENT.fullmatch(value)
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
    ):
        return None
    return value


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _same_or_within(path: Path, root: Path) -> bool:
    return path.resolve(strict=False) == root.resolve(strict=False) or _within(path, root)


def _registered_worktrees(repo: Path) -> set[Path] | None:
    listing = _git(repo, "worktree", "list", "--porcelain")
    if listing is None:
        return None
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }


def _registered_worktree_mismatch(
    repo: Path, worktree: Path, expected_revision: str | None
) -> str | None:
    if not expected_revision:
        return "WORKTREE_BASELINE_UNKNOWN"
    top_level = _git(worktree, "rev-parse", "--show-toplevel")
    repo_common_dir = _git_common_dir(repo)
    worktree_common_dir = _git_common_dir(worktree)
    if (
        top_level is None
        or Path(top_level).resolve() != worktree.resolve()
        or repo_common_dir is None
        or worktree_common_dir is None
        or repo_common_dir != worktree_common_dir
    ):
        return "WORKTREE_REPOSITORY_MISMATCH"
    if _git(worktree, "rev-parse", "HEAD") != expected_revision:
        return "WORKTREE_REVISION_MISMATCH"
    return None


def _git_common_dir(checkout: Path) -> Path | None:
    common_dir = _git(checkout, "rev-parse", "--git-common-dir")
    if common_dir is None:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = checkout / path
    return path.resolve()


def _unregistered_checkout_blocker(path: Path) -> str | None:
    git_marker = path / ".git"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return "WORKTREE_CHECKOUT_QUERY_FAILED"
    if result.returncode == 0 or git_marker.exists() or git_marker.is_symlink():
        return "WORKTREE_CHECKOUT_PRESENT"
    if "not a git repository" not in result.stderr.lower():
        return "WORKTREE_CHECKOUT_QUERY_FAILED"
    return None


def _remove_empty_parents(start: Path, root: Path) -> None:
    current = start
    while _same_or_within(current, root):
        try:
            current.rmdir()
        except OSError:
            break
        if current == root:
            break
        current = current.parent


@contextmanager
def _ownership_operation_lock(root: Path):
    lock_path = root.parent / f".{root.name}.tom-autodev-ownership.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ownership_database(root: Path) -> Path:
    return root.parent / f".{root.name}.tom-autodev-ownership.sqlite"


def _created(
    repo: Path,
    worktree: Path,
    run_id: str,
    task_id: str,
    revision: str,
    branch: str | None,
    user_changes: list[str],
    owner_token: str,
) -> dict[str, Any]:
    return {
        "status": "CREATED",
        "reason_code": None,
        "run_id": run_id,
        "task_id": task_id,
        "repo_path": str(repo),
        "worktree_path": str(worktree.resolve()),
        "baseline_revision": revision,
        "branch": branch,
        "user_changes": user_changes,
        "lock_key": f"{repo}:{task_id}",
        "baseline_status": "READY",
        "owner_token": owner_token,
    }


def _reconcile_result(
    ownership: sqlite3.Row,
    status: str,
    reason_code: str | None,
    ownership_status: str,
    action: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "ownership_status": ownership_status,
        "action": action,
        "owner_token": ownership["owner_token"],
        "run_id": ownership["run_id"],
        "task_id": ownership["task_id"],
        "repo_path": ownership["repo_path"],
        "worktree_path": ownership["worktree_path"],
        "baseline_revision": ownership["baseline_revision"],
    }


def _create_failure(
    repo: Path,
    run_id: str,
    task_id: str,
    reason_code: str,
    owner_token: str | None = None,
) -> dict[str, Any]:
    result = {
        "status": "BLOCKED",
        "reason_code": reason_code,
        "run_id": run_id,
        "task_id": task_id,
        "repo_path": str(repo),
        "worktree_path": None,
    }
    if owner_token is not None:
        result["owner_token"] = owner_token
    return result
