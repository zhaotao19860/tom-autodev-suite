"""Which control-plane code a long-running process was started with.

The watchers run for days under `supervise.sh`. A commit to the controller changes the
workflow spec, approval rules or runtime contracts underneath them, and a watcher still
executing the old code then judges live runs by rules the repository no longer has (on
2026-10-25 a stale watcher logged 84k false `WORKFLOW_SPEC_DRIFT` entries; it happened
again on 10-27). The fix is not to remember to restart it: the process notices that its
code changed and exits, and the supervisor starts the new code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent


def fingerprint(root: Path | None = None) -> str:
    """Content hash of every non-test control-plane source file and schema.

    Covers `*.py` under the scripts directory (tests excluded) and, for the installed
    controller, the sibling `schemas/*.json` it validates artifacts against. Project
    profiles are pinned per run and are deliberately not part of it.

    Content, not mtimes or git HEAD: a checkout of the same commit, or a touch, is not a
    change, while an uncommitted edit is one -- the process would be running code that
    differs from what is on disk either way.
    """
    base = Path(root) if root is not None else _ROOT
    digest = hashlib.sha256()
    files = sorted(base.rglob("*.py"))
    if root is None:
        files += sorted((base.parent / "schemas").glob("*.json"))
    for path in files:
        relative = Path(*path.relative_to(base.parent).parts) if root is None else path.relative_to(base)
        if relative.parts and relative.parts[0 if root is not None else 1] in {"tests", "__pycache__"}:
            continue
        digest.update(str(relative).encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            # A file removed between listing and reading is itself a change.
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def changed_since(started: str, root: Path | None = None) -> Callable[[], bool]:
    """A check the watch loops call between rounds: True once the code differs."""
    return lambda: fingerprint(root) != started
