from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any


_UNAVAILABLE = "WORKSPACE_COMMIT_UNAVAILABLE"
_PHASE_PREFIXES = {
    "GRILL": ("docs/", "repos/"),
    "SPEC": ("docs/", "repos/"),
    "TASKS": ("docs/", "repos/"),
    "PLAN": ("docs/development/", "docs/testing/", "repos/"),
    "IMPLEMENT": ("docs/development/", "docs/testing/", "repos/"),
    "REVIEW": ("docs/development/", "docs/testing/", "repos/"),
    "DIAGNOSE": ("docs/environment/", "docs/testing/", "repos/"),
}


def pinned(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("state") == "INTAKE":
            payload = event.get("payload")
            pin = payload.get("knowledge_workspace") if isinstance(payload, dict) else None
            return pin if isinstance(pin, dict) else None
    return None


def _git(pin: dict[str, Any], *arguments: str) -> str | None:
    if not all(isinstance(pin.get(key), str) and pin[key] for key in ("repository", "path", "commit")):
        return None
    try:
        return subprocess.run(
            ["git", "-C", str(Path(pin["path"]).expanduser()), *arguments],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def verify(pin: dict[str, Any]) -> str | None:
    result = _git(pin, "cat-file", "-e", f"{pin.get('commit')}^{{commit}}")
    return _UNAVAILABLE if result is None else None


def documents(pin: dict[str, Any], phase: str) -> list[dict[str, str]]:
    listing = _git(pin, "ls-tree", "-r", "--name-only", "-z", pin.get("commit", ""))
    if listing is None:
        raise ValueError(_UNAVAILABLE)
    prefixes = _PHASE_PREFIXES.get(phase, ())
    return [
        {
            "path": name,
            "commit": pin["commit"],
            "show": shlex.join([
                "git", "-C", str(Path(pin["path"]).expanduser()), "show", f"{pin['commit']}:{name}",
            ]),
        }
        for name in sorted(listing.split("\0"))
        if name.startswith(prefixes) and name.endswith(".md") and name.rsplit("/", 1)[-1] != "template.md"
    ]
