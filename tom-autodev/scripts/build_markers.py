"""Durable markers that take an owned iPipe build out of the run's evidence.

Two independent markers exist, both written to the state store's idempotency table:

* ``ipipe.build-superseded:{run}:{build}`` -- `ipipe-rebuild` replaced the build with a
  later build of the same revisions (``ipipe_rebuild.rebind``).
* ``ipipe.build-rejected:{run}:{build}`` -- the run triggered the build but refused to
  adopt it (for example TRIGGER_MODE_MISMATCH: the platform started a build of a
  different trigger mode than the approval authorized).

A marked build stays in the audit trail, but no module verdict, release selection,
RELEASE predecessor, G9 binding, watcher, discovery or product lookup may treat it as
the run's current build. Every caller asks through this module so the rule has one
definition.
"""
from __future__ import annotations

from typing import Any


def superseded_key(run_id: str, build_id: Any) -> str:
    return f"ipipe.build-superseded:{run_id}:{build_id}"


def rejected_key(run_id: str, build_id: Any) -> str:
    return f"ipipe.build-rejected:{run_id}:{build_id}"


def _marked(state: Any, key: str, build_id: str) -> bool:
    record = state.idempotency_result(key)
    return isinstance(record, dict) and str(record.get("build_id") or "") == build_id


def is_superseded(state: Any, run_id: str, build_id: Any) -> bool:
    """Whether this run replaced `build_id` with a later build (ipipe-rebuild)."""
    build = str(build_id or "")
    return bool(build) and _marked(state, superseded_key(run_id, build), build)


def is_rejected(state: Any, run_id: str, build_id: Any) -> bool:
    """Whether this run refused to adopt `build_id` after triggering it."""
    build = str(build_id or "")
    return bool(build) and _marked(state, rejected_key(run_id, build), build)


def is_excluded(state: Any, run_id: str, build_id: Any) -> bool:
    """A build that may not serve as current evidence: superseded or rejected."""
    return is_superseded(state, run_id, build_id) or is_rejected(state, run_id, build_id)
