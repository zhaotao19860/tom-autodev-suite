"""Re-pin a run to an edited project profile, once, under approval.

`_runtime_profile` compares the profile file's current sha256 against the hash
pinned at INTAKE and refuses the run on any drift. That guard exists because every
approval a run collected was granted against the facts in that file, so silent
drift would retroactively change what was approved.

Restarting the run is not the only way to honour that. This module lets the pin
move *explicitly*: the caller proves it holds the bytes that were pinned, the diff
is restricted to keys nothing has hashed yet, and one approval records the move.
Everything already produced — spec, DAG, change sets, reviews — stays valid,
because none of it depended on the keys that are allowed to change.

The pin is stored as an operation result rather than a state event: re-pinning is
not a phase transition, and forging one would put a state in the ledger that the
transition policy never authorised.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from project_registry import load_profile

_REPIN_KEY = "profile-repin"
ACTION = "PROFILE_REPIN"
# Only the pipeline registration may move. Every other top-level key is either
# hashed into an existing binding (`environment_profile` becomes the environment
# fingerprint) or names something a produced artifact already points at
# (`business_repos`, `test_repo`, `approval_channels`, `knowledge_sources`).
_ALLOWED_KEYS = frozenset({"pipeline_profile"})


def pinned_hash(events: list[dict[str, Any]], repin: Any) -> str | None:
    """The hash the run is currently pinned to: the re-pin if one landed, else INTAKE."""
    if isinstance(repin, dict) and isinstance(repin.get("new_hash"), str):
        return repin["new_hash"]
    intake = events[0].get("payload") if events else None
    recorded = intake.get("profile_hash") if isinstance(intake, dict) else None
    return recorded if isinstance(recorded, str) and recorded else None


def record(orchestrator: Any, run_id: str) -> Any:
    return record_for(orchestrator.state, run_id)


def record_for(state: Any, run_id: str) -> Any:
    """The landed re-pin, read straight from a state store.

    `phase_protocol` keeps its own copy of the pin check and only has the state
    store to hand, so both checks have to be able to reach this record. A pin that
    only one of them honours is worse than no re-pin at all: the run would look
    healthy through one door and conflicted through the other.
    """
    return state.idempotency_result(f"{_REPIN_KEY}:{run_id}")


def plan(orchestrator: Any, run_id: str, previous_path: str | Path) -> dict[str, Any]:
    """Say whether the edit is allowed, and what has to be approved to accept it."""
    events = orchestrator.state.events(run_id)
    if not events:
        return _failure("RUN_NOT_FOUND", run_id)
    intake = events[0].get("payload")
    if not isinstance(intake, dict) or not isinstance(intake.get("profile_path"), str):
        return _failure("PROJECT_NOT_READY", run_id)
    old_hash = pinned_hash(events, record(orchestrator, run_id))
    if old_hash is None:
        return _failure("PROJECT_NOT_READY", run_id)
    current_path = Path(intake["profile_path"])
    try:
        current_bytes = current_path.read_bytes()
        previous_bytes = Path(previous_path).expanduser().read_bytes()
    except OSError as error:
        return _failure("PROFILE_UNREADABLE", run_id, detail=str(error))
    new_hash = hashlib.sha256(current_bytes).hexdigest()
    if new_hash == old_hash:
        return _failure("NOTHING_TO_REPIN", run_id, old_hash=old_hash)
    if hashlib.sha256(previous_bytes).hexdigest() != old_hash:
        # Without the pinned bytes there is nothing to diff against, and a re-pin
        # that cannot name what changed is indistinguishable from accepting drift.
        return _failure("PREVIOUS_PROFILE_MISMATCH", run_id, old_hash=old_hash)
    loaded = load_profile(current_path)
    if not loaded.get("ready"):
        return {"ok": False, "run_id": run_id, **loaded}
    new_profile = loaded["profile"]
    try:
        old_profile = yaml.safe_load(previous_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        return _failure("PREVIOUS_PROFILE_INVALID", run_id, detail=str(error))
    if not isinstance(old_profile, dict):
        return _failure("PREVIOUS_PROFILE_INVALID", run_id)
    changed = sorted(
        key for key in set(old_profile) | set(new_profile)
        if old_profile.get(key) != new_profile.get(key)
    )
    if not changed:
        # Same content, different bytes: formatting only. Still a re-pin, but there is
        # nothing to review, so it is reported rather than hidden.
        changed = ["<formatting-only>"]
    forbidden = [key for key in changed if key not in _ALLOWED_KEYS and key != "<formatting-only>"]
    if forbidden:
        return _failure("REPIN_FIELD_FORBIDDEN", run_id, forbidden=forbidden, changed=changed)
    if new_profile.get("project_id") != intake.get("project"):
        return _failure("PROJECT_PROFILE_MISMATCH", run_id)
    broken = _broken_submission_bindings(orchestrator, run_id, new_profile)
    if broken:
        return _failure("REPIN_BREAKS_SUBMISSION_BINDING", run_id, bindings=broken)
    return {
        "ok": True,
        "reason_code": "OK",
        "run_id": run_id,
        "action": ACTION,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "changed_keys": changed,
        "input_hash": _input_hash(run_id, old_hash, new_hash, changed),
    }


def apply(orchestrator: Any, run_id: str, approval_id: str, previous_path: str | Path) -> dict[str, Any]:
    """Move the pin, once, against an approval bound to this exact move."""
    key = f"{_REPIN_KEY}:{run_id}"
    prepared = plan(orchestrator, run_id, previous_path)
    existing = record(orchestrator, run_id)
    if prepared.get("reason_code") == "NOTHING_TO_REPIN" and isinstance(existing, dict):
        # The pin already moved: the file now matches it, so this is a replay.
        return {"ok": True, "reason_code": "ALREADY_REPINNED", "run_id": run_id, **existing}
    if not prepared.get("ok"):
        return prepared
    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict):
        return _failure("APPROVAL_REQUIRED", run_id)
    if approval.get("run_id") != run_id or approval.get("action") != ACTION:
        return _failure("APPROVAL_GATE_MISMATCH", run_id)
    if approval.get("input_hash") != prepared["input_hash"]:
        return _failure("APPROVAL_INPUT_MISMATCH", run_id)
    if approval.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED", run_id)
    stored = {
        "old_hash": prepared["old_hash"],
        "new_hash": prepared["new_hash"],
        "changed_keys": prepared["changed_keys"],
        "input_hash": prepared["input_hash"],
        "approval_id": approval_id,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    orchestrator.state.save_idempotency_result(key, stored)
    return {"ok": True, "reason_code": "OK", "run_id": run_id, **stored}


def request(
    orchestrator: Any,
    run_id: str,
    previous_path: str | Path,
    *,
    comate_client: Any,
    infoflow_client: Any,
) -> dict[str, Any]:
    """Open the re-pin gate.

    This cannot go through the generic `request-approval`: that path builds its member
    policy from `_runtime_profile`, which is exactly what the drifted pin makes fail,
    so the gate that repairs the conflict would be unreachable while the conflict
    lasts. The approvers are read from the *pinned* copy instead of the edited file —
    the people who should decide are the ones the run was pinned with.
    """
    prepared = plan(orchestrator, run_id, previous_path)
    if not prepared.get("ok"):
        return prepared
    try:
        previous = yaml.safe_load(Path(previous_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return _failure("PREVIOUS_PROFILE_INVALID", run_id, detail=str(error))
    channels = previous.get("approval_channels") if isinstance(previous, dict) else None
    role_members = channels.get("role_members") if isinstance(channels, dict) else None
    if not isinstance(role_members, dict):
        return _failure("MEMBER_CONFIRMATION_REQUIRED", run_id)
    members = sorted({
        email for values in role_members.values() if isinstance(values, list) for email in values
    })
    if not members:
        return _failure("MEMBER_CONFIRMATION_REQUIRED", run_id)
    opened = orchestrator.request_infoflow_approval(
        run_id,
        ACTION,
        prepared["input_hash"],
        member_policy={"comate": members, "infoflow": members},
        evidence={
            "gate": {
                "subject": "把运行重新钉到改过的 project profile",
                "effect": f"profile_hash {prepared['old_hash'][:12]}… → {prepared['new_hash'][:12]}…",
            },
            "changed_keys": prepared["changed_keys"],
            "old_hash": prepared["old_hash"],
            "new_hash": prepared["new_hash"],
        },
        comate_client=comate_client,
        infoflow_client=infoflow_client,
    )
    return {**prepared, "approval": opened}


def _broken_submission_bindings(
    orchestrator: Any, run_id: str, new_profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Submissions already pinned a pipeline and release rule into their binding.

    Re-pinning past one of those would leave an artifact whose `controller_binding`
    no longer describes anything in the profile, and `phase_protocol` compares that
    binding field by field before it will accept IPIPE evidence.
    """
    pipeline = new_profile.get("pipeline_profile")
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    offered = {str(pipeline.get("pipeline_id") or "")}
    rules_by_pipeline = {str(pipeline.get("pipeline_id") or ""): pipeline.get("release_rule")}
    for entry in pipeline.get("pipelines") or []:
        if isinstance(entry, dict) and entry.get("pipeline_id"):
            pipeline_id = str(entry["pipeline_id"])
            offered.add(pipeline_id)
            rules_by_pipeline[pipeline_id] = entry.get("release_rule", pipeline.get("release_rule"))
    broken = []
    for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
        if artifact.get("kind") != "submission":
            continue
        binding = (artifact.get("metadata") or {}).get("controller_binding")
        if not isinstance(binding, dict):
            continue
        pipeline_id = str(binding.get("pipeline_id") or "")
        if pipeline_id not in offered or binding.get("release_rule") != rules_by_pipeline.get(pipeline_id):
            broken.append({
                "artifact_id": artifact.get("artifact_id"),
                "pipeline_id": binding.get("pipeline_id"),
            })
    return broken


def _input_hash(run_id: str, old_hash: str, new_hash: str, changed: list[str]) -> str:
    payload = {
        "run_id": run_id, "action": ACTION,
        "old_hash": old_hash, "new_hash": new_hash, "changed_keys": changed,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _failure(reason_code: str, run_id: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "run_id": run_id, **details}
