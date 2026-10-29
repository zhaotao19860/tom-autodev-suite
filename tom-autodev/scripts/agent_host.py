"""Which code agent is driving the run, and where its skills live (agent-neutral).

The controller does not depend on any one host. Comate, Claude Code and Codex all read
the same skill directories and produce the same DraftContent through `submit-draft`; what
differs is where each installs its skills and how it calls a Stop hook. This module is the
one place that knows those paths.

Resolution order for a skill directory:
1. an explicit environment override (`TOM_AUTODEV_<NAME>_SKILL_DIR`);
2. the active agent's skill roots (`TOM_AUTODEV_AGENT`, else detected from the
   environment the agent exports);
3. every known agent's skill roots, in a fixed order.
"""

from __future__ import annotations

import os
from pathlib import Path

# The stable host label written into artifact envelopes. It names the controller
# contract, not the agent: every agent produces the same envelope.
ENVELOPE_HOST = "comate"

AGENTS = ("comate", "claude", "codex")


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def skill_roots(agent: str) -> list[Path]:
    home = _home()
    return {
        "comate": [home / ".comate" / "skills", home / ".comate" / "skills" / ".system"],
        "claude": [home / ".claude" / "skills"],
        "codex": [home / ".codex" / "skills", home / ".agents" / "skills"],
    }.get(agent, [])


def active_agent() -> str | None:
    """The agent named by TOM_AUTODEV_AGENT, else the one whose environment is present."""
    named = (os.environ.get("TOM_AUTODEV_AGENT") or "").strip().lower()
    if named in AGENTS:
        return named
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    if os.environ.get("COMATE_SKILL_DIR") or os.environ.get("COMATE_USERNAME"):
        return "comate"
    return None


def search_roots() -> list[Path]:
    first = active_agent()
    order = ([first] if first else []) + [agent for agent in AGENTS if agent != first]
    roots: list[Path] = []
    for agent in order:
        roots.extend(skill_roots(agent))
    return roots


def find_skill(name: str) -> Path | None:
    """The first installed directory for skill `name` (e.g. "icode", "ku-doc-manage")."""
    override = os.environ.get(f"TOM_AUTODEV_{name.upper().replace('-', '_')}_SKILL_DIR")
    if override:
        return Path(override).expanduser()
    for root in search_roots():
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def skill_path(name: str) -> str:
    """`find_skill`, or the Comate default location so error messages name a real path."""
    found = find_skill(name)
    return str(found if found is not None else _home() / ".comate" / "skills" / ".system" / name)


def session_id() -> str | None:
    """The id of the agent session running this process, when the agent exports one."""
    for name in ("TOM_AUTODEV_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID",
                 "COMATE_SESSION_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def bind_driver(state: object, run_id: str) -> None:
    """Record which agent session drives `run_id`; the newest driving command wins.

    Records are append-only (`run.driver:<run>:<utc>:<session>`), so the store's
    write-once contract holds and the history of drivers stays readable.
    """
    from datetime import datetime, timezone
    session = session_id()
    if not session or not isinstance(run_id, str) or not run_id:
        return
    at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    state.save_idempotency_result(f"run.driver:{run_id}:{at}:{session}", {
        "run_id": run_id, "session_id": session, "agent": active_agent(), "at": at})


def driver_session(state: object, run_id: str) -> str | None:
    """The session of the newest driving command for `run_id`, if any was recorded."""
    reader = getattr(state, "idempotency_results_with_prefix", None)
    rows = reader(f"run.driver:{run_id}:") if callable(reader) else []
    if not rows:
        return None
    _key, record = max(rows, key=lambda row: row[0])
    return record.get("session_id") if isinstance(record, dict) else None


def install_destinations() -> list[Path]:
    """Where `install_links` places the suite's skills: one root per agent."""
    return [skill_roots(agent)[0] for agent in AGENTS]


def hook_config_paths() -> list[Path]:
    """Hook configuration files that may register the Stop hook, across agents."""
    home = _home()
    return [
        home / ".comate" / "hooks.json",
        home / ".comate" / "hooks.local.json",
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        home / ".codex" / "hooks.json",
    ]
