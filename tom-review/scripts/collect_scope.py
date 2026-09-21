#!/usr/bin/env python3
"""Read a pinned two-commit review scope without checkout, fetch or project execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _git(repo: Path, *args: str, attr_source: str | None = None) -> bytes:
    env = os.environ.copy()
    # Caller environment must not redirect -C to another index/repository.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1")
    if attr_source:
        env["GIT_ATTR_SOURCE"] = attr_source
    command = [
        "git", "--no-pager", "--no-optional-locks", "--no-replace-objects",
        "-c", "core.fsmonitor=false", "-c", "core.attributesFile=" + os.devnull,
        "-C", str(repo), *args,
    ]
    try:
        result = subprocess.run(command, capture_output=True, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("scope collection failed or exceeded its 60s command bound") from error
    if result.returncode:
        # Avoid emitting arbitrary repository configuration or external credential text.
        raise ValueError("Git could not read the requested repository/commit scope")
    return result.stdout


def _commit(repo: Path, revision: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision):
        raise ValueError("use the full commit object ID from the pinned input, not a moving ref")
    if _git(repo, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("a pinned revision must identify a commit object")
    return revision.lower()


def collect_scope(repo: str | Path, baseline: str, candidate: str) -> dict:
    repo = Path(repo).resolve()
    root = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n")))
    baseline, candidate = _commit(root, baseline), _commit(root, candidate)
    # No similarity guesses: a rename is an explicit delete + add. Both sides survive.
    options = (
        "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
        "--diff-algorithm=myers", "--no-indent-heuristic", "--ignore-submodules=none",
    )
    raw = _git(root, "diff", "--raw", "-z", *options, baseline, candidate, "--")
    tokens = raw.split(b"\0")
    if tokens.pop() != b"" or len(tokens) % 2:
        raise ValueError("incomplete Git scope output")
    files = []
    for pos in range(0, len(tokens), 2):
        metadata = tokens[pos].split()
        if len(metadata) != 5 or not metadata[0].startswith(b":"):
            raise ValueError("unexpected Git raw diff record")
        old_mode, new_mode, _, _, status = metadata
        files.append({
            "path": os.fsdecode(tokens[pos + 1]), "status": status.decode("ascii"),
            "old_mode": old_mode[1:].decode("ascii"), "new_mode": new_mode.decode("ascii"),
        })
    stats = _git(root, "diff", "--numstat", "-z", *options, baseline, candidate, "--",
                 attr_source=candidate)
    counts = {}
    for record in stats.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise ValueError("unexpected Git numstat record")
        added, deleted, path = parts
        binary = added == b"-" or deleted == b"-"
        counts[os.fsdecode(path)] = {
            "added": None if binary else int(added),
            "deleted": None if binary else int(deleted), "binary": binary,
        }
    if {entry["path"] for entry in files} != counts.keys():
        raise ValueError("file inventory and line statistics disagree; scope is incomplete")
    for entry in files:
        entry.update(counts[entry["path"]])
    return {
        "repository": str(root), "baseline_revision": baseline,
        "candidate_revision": candidate, "files": files,
        # Even `git status` can execute a repository's clean filter. The fixed scope
        # needs no worktree content/status inspection, so omit that operation entirely.
        "worktree_changes_ignored": True, "worktree_state": "not_inspected",
        "rename_policy": "delete-and-add",
        "statistics_policy": "Git numstat; attributes may affect binary classification; advisory only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True, help="full baseline commit ID")
    parser.add_argument("--head", required=True, help="full candidate commit ID")
    args = parser.parse_args()
    try:
        result = collect_scope(args.repo, args.base, args.head)
    except ValueError as error:
        print(json.dumps({"error": "SCOPE_INCOMPLETE", "reason": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
