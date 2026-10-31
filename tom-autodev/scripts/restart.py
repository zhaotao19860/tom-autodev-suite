"""Contract 3.4: restart a requirement in a new run, with lineage (MVP_CONVERGENCE_PLAN §3.4).

`plan` is read-only: it lists every remote object the source left and the disposition
each has, and says whether a restart is allowed. `execute` re-checks the same plan and
commits, in one transaction, the source's STOPPED event (when it was not stopped), the
lineage row and the target's INTAKE. A source restarts once; the same key replays.

What blocks (`RESTART_SOURCE_UNRESOLVED`):
- an intent without a receipt, or one whose state is UNKNOWN/AMBIGUOUS (side-channel
  KU/iCafe writes under evidence contract v2 excepted, they never block a phase);
- a CR of the source that is merged (needs a recorded disposition, `--accept-merged`);
- a CR still open whose disposition is not given (`--keep-open-crs` records
  KEPT_FOR_REFERENCE; abandoning it on iCode first makes it ABANDONED_CONFIRMED);
- a build of the source still running;
- a source worktree with uncommitted changes.

Nothing is copied: the target starts at INTAKE and goes through GRILL/SPEC/TASKS and
their gates again. The source's requirement snapshot and design artifacts are recorded
in the lineage only as references (artifact id + hash). The source's confirmed repair
rounds are carried to the target's INTAKE (`inherited_repair_rounds`), so a restart does
not refill the CODE_FIX budget; environment retries are not carried.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_SIDE_CHANNEL = frozenset({"icafe", "ku", "knowledge"})
# Only a platform-reported end is a disposition; anything else (running, manual wait,
# unknown, unreadable) blocks the restart (C-05).
_ENDED = frozenset({"SUCCESS", "SUCCEEDED", "SUCC", "PASSED", "PASS", "FAIL", "FAILED", "ERROR",
                    "ABORTED", "CANCELLED", "CANCEL", "SKIPPED", "SKIP"})
_DESIGN_PHASES = ("GRILL", "SPEC", "TASKS")


def _open_intents(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    import intent_executor
    import workflow_spec

    events = orchestrator.state.events(run_id)
    side_channel = workflow_spec.run_evidence_contract(events) == "v2"
    blocking = []
    for intent in orchestrator.state.all_intents(run_id):
        operation = str(intent.get("operation") or "")
        if side_channel and operation.split(".", 1)[0] in _SIDE_CHANNEL:
            continue
        if operation.startswith("approval."):
            continue
        state = intent_executor.intent_state(orchestrator.state, intent)
        if state in {intent_executor.CLAIMED, intent_executor.UNKNOWN, intent_executor.AMBIGUOUS}:
            blocking.append({"intent_id": intent["intent_id"], "operation": operation, "state": state})
    return blocking


def _submitted_crs(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    """The newest confirmed submission receipt per CR of the source."""
    found: dict[str, dict[str, Any]] = {}
    for item in orchestrator.state.external_results(run_id):
        intent = item.get("intent") or {}
        response = (item.get("receipt") or {}).get("response")
        if intent.get("operation") != "icode.submit" or not isinstance(response, dict):
            continue
        number = str(response.get("change_number") or "")
        if response.get("ok") is True and number:
            found[number] = {"change_number": number, "module": response.get("module"),
                             "repo_path": response.get("repo_path"),
                             "change_id": response.get("change_id")}
    return sorted(found.values(), key=lambda item: item["change_number"])


def _cr_status(cr: dict[str, Any], query: Any) -> str | None:
    """NEW / MERGED / ABANDONED from iCode `get_review_info`, or None when unproven.

    The repository listings are paged (20 per status, `hasMore`), so a CR beyond the
    first page reads as absent; the per-CR lookup is exact.
    """
    if query is not None:
        return query(cr)
    import json

    for candidate in ("/Users/tom/.icode/bin/icode-cli", "icode-cli"):
        try:
            completed = subprocess.run(
                [candidate, "api", "get_review_info", "--change-number", cr["change_number"], "-o", "json"],
                cwd=str(cr.get("repo_path") or "."), capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        try:
            decoded = json.loads(completed.stdout)
        except ValueError:
            return None
        data = decoded.get("data", decoded) if isinstance(decoded, dict) else {}
        if not isinstance(data, dict) or str(data.get("_number") or "") != cr["change_number"]:
            return None
        status = str(data.get("status") or "").upper()
        return status if status in {"NEW", "MERGED", "ABANDONED"} else None
    return None


def _builds(orchestrator: Any, run_id: str, status_of: Any) -> list[dict[str, Any]]:
    import build_markers

    rows = orchestrator.state.idempotency_results_with_prefix(f"ipipe.build-binding:{run_id}:")
    builds = []
    for key, _record in rows:
        build_id = key.rsplit(":", 1)[-1]
        # Excluded only means "not evidence"; the build was still created on the platform
        # and must be ended like any other (C-05, RSTR-001).
        builds.append({"build_id": build_id, "status": status_of(build_id) if status_of else None,
                       "excluded": build_markers.is_excluded(orchestrator.state, run_id, build_id)})
    return builds


def _dirty_worktrees(orchestrator: Any, run_id: str) -> list[str]:
    from submit_descriptor import _ownership_rows

    dirty = []
    for row in _ownership_rows(orchestrator, run_id).values():
        path = Path(str(row.get("worktree_path") or ""))
        if not path.is_dir():
            continue
        completed = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                                   capture_output=True, text=True)
        if completed.returncode != 0 or completed.stdout.strip():
            dirty.append(str(path))
    return sorted(dirty)


def _design_references(orchestrator: Any, run_id: str) -> list[dict[str, Any]]:
    references = []
    for phase in _DESIGN_PHASES:
        latest = orchestrator.artifacts.latest_phase(run_id, phase, None)
        if latest.get("valid"):
            envelope = latest.get("envelope") or {}
            references.append({"phase": phase, "artifact_id": latest.get("artifact_id"),
                               "content_hash": envelope.get("content_hash")})
    return references


def _repair_rounds(orchestrator: Any, run_id: str) -> int:
    """CODE_FIX rounds the source spent: confirmed REPAIR diagnoses plus every inherited round.

    Environment diagnoses (route IPIPE, environment classes) are not CODE_FIX and are not
    carried (MVP §3.4, RSTR-004); rounds inherited through `adopt-code-only-repair` or an
    earlier restart are, wherever their event is (C-06).
    """
    import repair_policy

    rounds = 0
    for artifact in orchestrator.artifacts.phase_artifacts(run_id, "DIAGNOSE"):
        content = ((artifact.get("envelope") or {}).get("content") or {}) if artifact.get("valid") else {}
        if (content.get("evidence_state") == "SUFFICIENT" and content.get("route") == "REPAIR"
                and content.get("classification") not in repair_policy.ENVIRONMENT_FAILURE_CLASSES):
            rounds += 1
    for event in orchestrator.state.events(run_id):
        payload = event.get("payload") or {}
        if payload.get("adoption_kind") == "CODE_ONLY_REPAIR" or (
                event.get("state") == "INTAKE" and payload.get("restart_of")):
            inherited = payload.get("inherited_repair_rounds")
            if isinstance(inherited, int) and not isinstance(inherited, bool):
                rounds += inherited
    return rounds


_DIFF_FIELDS = ("title", "body", "acceptance", "type", "fields")


def requirement_diff(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any] | None:
    """What changed in the card between the source run's snapshot and the new one (§3.4).

    Scope and acceptance are what the GRILL decided on; any change there is a diff the
    design approval has to see. None when they are the same.
    """
    import hashlib
    import json

    changed = {}
    for field in _DIFF_FIELDS:
        before, after = source.get(field), target.get(field)
        if before != after:
            changed[field] = {"before": before, "after": after}
    if not changed:
        return None
    body = {"source_snapshot_hash": source.get("content_hash"),
            "target_snapshot_hash": target.get("content_hash"), "changed": changed}
    return {**body, "content_hash": hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def plan(
    orchestrator: Any, source_run_id: str, *, keep_open_crs: bool = False,
    accept_merged: bool = False, cr_status: Any = None, build_status: Any = None,
) -> dict[str, Any]:
    """Read-only: every leftover remote object of the source and whether restart may go."""
    events = orchestrator.state.events(source_run_id)
    if not events:
        return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": source_run_id}
    intake = events[0].get("payload") or {}
    state = events[-1].get("state")
    if state == "RELEASE_SUCCESS":
        return {"ok": False, "reason_code": "RESTART_SOURCE_RELEASED", "run_id": source_run_id}
    held = orchestrator.state.lineage(source_run_id)
    if held is not None and held["source_run_id"] == source_run_id:
        return {"ok": True, "reason_code": "RESTART_REPLAY", "run_id": source_run_id,
                "target_run_id": held["target_run_id"], "lineage": held["payload"]}
    blockers: list[dict[str, Any]] = []
    # The exact intent set this check looked at; the commit refuses if it changed.
    checked_intent_ids = sorted(item["intent_id"] for item in orchestrator.state.all_intents(source_run_id))
    intents = _open_intents(orchestrator, source_run_id)
    if intents:
        blockers.append({"kind": "intent", "items": intents})
    crs = []
    for cr in _submitted_crs(orchestrator, source_run_id):
        status = _cr_status(cr, cr_status)
        if status == "ABANDONED":
            disposition = "ABANDONED_CONFIRMED"
        elif status == "NEW" and keep_open_crs:
            disposition = "KEPT_FOR_REFERENCE"
        elif status == "MERGED" and accept_merged:
            disposition = "MERGED_ACCEPTED"
        else:
            disposition = {"NEW": "OPEN_UNDECIDED", "MERGED": "MERGED_UNDECIDED"}.get(
                status, "ABANDONED_UNKNOWN")
            blockers.append({"kind": "cr", "change_number": cr["change_number"], "disposition": disposition})
        crs.append({**cr, "status": status, "disposition": disposition})
    builds = _builds(orchestrator, source_run_id, build_status)
    for build in builds:
        if str(build["status"] or "").upper() not in _ENDED:
            blockers.append({"kind": "build", "build_id": build["build_id"], "status": build["status"]})
    dirty = _dirty_worktrees(orchestrator, source_run_id)
    if dirty:
        blockers.append({"kind": "worktree", "paths": dirty})
    lineage = {
        "source_run_id": source_run_id,
        "requirement_id": intake.get("requirement_id"),
        "project": intake.get("project"),
        "source_state": state,
        "source_event_id": events[-1]["event_id"],
        "source_snapshot_hash": (intake.get("requirement_snapshot") or {}).get("content_hash"),
        "design_references": _design_references(orchestrator, source_run_id),
        "crs": [{k: v for k, v in cr.items() if k != "repo_path"} for cr in crs],
        "builds": builds,
        "inherited_repair_rounds": _repair_rounds(orchestrator, source_run_id),
    }
    if blockers:
        return {"ok": False, "reason_code": "RESTART_SOURCE_UNRESOLVED", "run_id": source_run_id,
                "blockers": blockers, "lineage": lineage}
    return {"ok": True, "reason_code": "RESTART_READY", "run_id": source_run_id, "lineage": lineage,
            "checked_intent_ids": checked_intent_ids}


def execute(
    orchestrator: Any, source_run_id: str, requirement_snapshot: dict[str, Any], *,
    reason: str, **options: Any,
) -> dict[str, Any]:
    """Commit the restart. The snapshot is re-fetched from iCafe by the caller."""
    if not isinstance(reason, str) or not reason.strip():
        return {"ok": False, "reason_code": "RESTART_REASON_REQUIRED", "run_id": source_run_id}
    from orchestrator import _under_run_lease

    # The same per-run lease the worker drives the source with: no worker can claim a new
    # external write between the check and the commit (C-04, RSTR-002).
    return _under_run_lease(orchestrator, source_run_id, lambda: _execute(
        orchestrator, source_run_id, requirement_snapshot, reason=reason, **options))


def _execute(
    orchestrator: Any, source_run_id: str, requirement_snapshot: dict[str, Any], *,
    reason: str, **options: Any,
) -> dict[str, Any]:
    planned = plan(orchestrator, source_run_id, **options)
    if planned.get("reason_code") == "RESTART_REPLAY" or not planned.get("ok"):
        return planned
    lineage = planned["lineage"]
    events = orchestrator.state.events(source_run_id)
    if events[-1]["event_id"] != lineage["source_event_id"]:
        return {"ok": False, "reason_code": "SOURCE_EVENT_MISMATCH", "run_id": source_run_id}
    checked_intents = planned["checked_intent_ids"]
    key = f"restart:{source_run_id}"
    source_snapshot = (events[0].get("payload") or {}).get("requirement_snapshot") or {}
    diff = requirement_diff(source_snapshot, requirement_snapshot)
    import workflow_spec

    change_class = None
    if diff is not None:
        # A changed requirement is clarified again by a person: a class that completes
        # GRILL automatically is replaced by the default one (RSTR-003, C-10).
        classified = workflow_spec.classify_change(requirement_snapshot, None)
        if workflow_spec.phase_mode(classified, "GRILL") == "auto":
            change_class = workflow_spec.DEFAULT_CHANGE_CLASS
    prepared = orchestrator.start(
        lineage["requirement_id"], lineage["project"], requirement_snapshot=requirement_snapshot,
        fresh_token=key, intake_extra={}, change_class=change_class,
        requirement_diff=diff,
    )
    if "payload" not in prepared:
        return {"ok": False, **prepared, "run_id": source_run_id}
    target_run_id = prepared["run_id"]
    lineage = {**lineage, "target_run_id": target_run_id, "reason": reason.strip(),
               "snapshot_changed": diff is not None,
               **({"requirement_diff_hash": diff["content_hash"]} if diff else {})}
    payload = {**prepared["payload"], "restart_of": source_run_id,
               "inherited_repair_rounds": lineage["inherited_repair_rounds"],
               "design_references": lineage["design_references"]}
    current = events[-1]
    stop_payload = None
    if current.get("state") != "STOPPED":
        stop_payload = {"previous_state": current.get("state"),
                        "policy_decision": {"allowed": True, "reason_code": "RESTART",
                                            "restart_target": target_run_id}}
    committed = orchestrator.state.commit_restart(
        source_run_id, lineage["source_event_id"], stop_payload, target_run_id, payload, key, lineage,
        prepared["idempotency_key"], prepared["result"], expected_intent_ids=checked_intents,
    )
    if committed["status"] == "REPLAY":
        return {"ok": True, "reason_code": "RESTART_REPLAY", "run_id": source_run_id,
                "target_run_id": committed["target_run_id"], "lineage": committed["lineage"]}
    if committed["status"] != "COMMITTED":
        return {"ok": False, "reason_code": committed["status"], "run_id": source_run_id}
    return {"ok": True, "reason_code": "RESTARTED", "run_id": source_run_id,
            "target_run_id": target_run_id, "event_id": committed["event_id"], "lineage": lineage}
