from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def discover_candidates(
    project_root: Path,
    *,
    icode: object,
    ipipe: object,
    mappings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser()
    try:
        remote = _git(root, "remote", "get-url", "origin")
        revision = _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return {
            "ready": False,
            "reason_code": "PROJECT_NOT_READY",
            "missing": ["project_repository"],
            "invalid": [],
        }

    repository = {"path": str(root), "remote": remote, "revision": revision}
    icode_candidates = _discover(icode, remote, revision)
    ipipe_candidates = _discover(ipipe, remote, revision)
    test_repositories = _mapped_test_repositories(mappings, remote, revision)
    candidate_groups = {
        "icode_candidates": icode_candidates,
        "ipipe_candidates": ipipe_candidates,
        "test_repositories": test_repositories,
    }
    missing = [name for name, candidates in candidate_groups.items() if not candidates]
    ambiguous = any(len(candidates) > 1 for candidates in candidate_groups.values())
    reason_code = "PROFILE_CONFIRMATION_REQUIRED" if ambiguous else "PROJECT_NOT_READY" if missing else "READY"
    return {
        "ready": reason_code == "READY",
        "reason_code": reason_code,
        "repository": repository,
        "icode_candidates": icode_candidates,
        "ipipe_candidates": ipipe_candidates,
        "test_repositories": test_repositories,
        "missing": missing,
        "invalid": [],
    }


def _git(project_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project_root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _discover(client: object, remote: str, revision: str) -> list[dict[str, Any]]:
    discover = getattr(client, "discover", None)
    if not callable(discover):
        return []
    result = discover(remote, revision)
    return result if isinstance(result, list) else []


def _mapped_test_repositories(
    mappings: dict[str, Any] | None, remote: str, revision: str
) -> list[dict[str, Any]]:
    configured = (mappings or {}).get("test_repositories", [])
    if not isinstance(configured, list):
        return []
    return [
        candidate
        for candidate in configured
        if isinstance(candidate, dict)
        and candidate.get("source_remote") == remote
        and candidate.get("source_revision", revision) == revision
    ]
