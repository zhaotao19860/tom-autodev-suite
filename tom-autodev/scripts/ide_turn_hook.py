#!/usr/bin/env python3
"""Session-stop hook: tell 如流 that a parked run is waiting for the IDE.

The notice used to hang off `orchestrator.py complete-phase`, so it only fired when a
phase happened to land through the CLI. Every other way a turn ends -- stopping to ask
a question, an error, a run driven through the Python API, a compacted session -- left
the operator with nothing. A stop hook is not conditional on any of that: the harness
runs it whenever the session stops, so the notice follows the stop rather than the
phase.

Never fails the stop. Anything unexpected is logged and swallowed, and stdout is left
empty so the harness has nothing to interpret.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIG_ROOT = Path.home() / ".tom-autodev"
_LOG = _CONFIG_ROOT / "ide-turn-hook.log"


def _log(payload: dict[str, object]) -> None:
    try:
        record = {"at": datetime.now(timezone.utc).isoformat(), **payload}
        with _LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    if not (_CONFIG_ROOT / "state.sqlite").is_file():
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from approval_watch import notify_every_parked_run
        from orchestrator import Orchestrator, _infoflow_notify_client

        orchestrator = Orchestrator()
        result = notify_every_parked_run(
            orchestrator, _infoflow_notify_client(orchestrator)
        )
        # Logged on every stop, not only when something goes out: this notifier's
        # success mode is silence, and an unobservable notifier is what let the missing
        # notice go unnoticed for a whole run. One short line per stop stays cheap.
        _log({"notices": result.get("notices") or []})
    except Exception as error:  # noqa: BLE001 - a missed notice must not block the stop
        _log({"error": type(error).__name__, "detail": str(error)[:400]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
