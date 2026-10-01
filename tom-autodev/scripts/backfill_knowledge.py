"""Backfill human-readable KU documents from durable phase artifacts.

This command is intentionally artifact-driven. It never invents a missing phase:
only integrity-checked envelopes already present in ArtifactStore are published.
The run root remains the inventory of the complete 00–11 document set and makes
unproduced phases visible without pretending they happened.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from artifact_store import ArtifactStore
from knowledge_sync import KnowledgeSync
from phase_document import render_phase_markdown
from project_registry import load_profile, requirement_profile_path, resolve_active_profile_path
from state_store import StateStore


_TITLES = {
    "INTAKE": "00-requirement-snapshot",
    "GRILL": "01-grill",
    "SPEC": "02-spec",
    "TASKS": "03-tasks",
    "PLAN": "04-task-plan",
    "IMPLEMENT": "05-change-set",
    "REVIEW": "06-review",
    "SUBMIT": "08-icode-submission",
    "IPIPE": "09-ipipe-evidence",
    "DIAGNOSE": "10-diagnosis",
    "RELEASE": "11-release-evidence",
}


def backfill(
    run_id: str,
    requirement_id: str,
    project: str,
    *,
    config_root: Path | str | None = None,
    username: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(config_root or (Path.home() / ".tom-autodev")).expanduser()
    profile_path = requirement_profile_path(project, requirement_id, root)
    if not profile_path.exists():
        profile_path = resolve_active_profile_path(project, requirement_id, root)
    profile_result = load_profile(profile_path)
    if not profile_result.get("ready"):
        return profile_result

    # Do not reuse the live run ledger: an old unresolved approval or unrelated
    # external intent must not block a read-only historical KU migration. Remote
    # KU markers provide the idempotency boundary for this command.
    temp_state_dir = tempfile.TemporaryDirectory(prefix="tom-autodev-ku-backfill-")
    state = StateStore(Path(temp_state_dir.name) / "state.sqlite")
    # This flag is local to this bounded command's StateStore instance. It does
    # not repin the run, alter the workflow hash, or make normal continuation
    # bypass drift checks.
    state.allow_historical_backfill = True
    artifacts = ArtifactStore(root / "artifacts")
    phases = artifacts.phase_artifacts(run_id)
    valid = [item for item in phases if item.get("valid") and item.get("envelope")]
    result: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "requirement_id": requirement_id,
        "profile_path": str(profile_path),
        "found": [],
        "published": [],
        "missing": [
            phase for phase in (
                "INTAKE", "GRILL", "SPEC", "TASKS", "WORKSPACE", "PLAN",
                "IMPLEMENT", "REVIEW", "SUBMIT", "IPIPE", "DIAGNOSE", "RELEASE",
            )
            if not any(item["envelope"].get("phase") == phase for item in valid)
        ],
    }
    for item in valid:
        envelope = item["envelope"]
        phase = envelope["phase"]
        content = envelope["content"]
        canonical = _canonical_json(content)
        base_title = _TITLES.get(phase, phase.lower())
        task_id = envelope.get("task_id")
        if task_id:
            base_title += f"/{task_id}"
        # Existing pages are immutable. A backfill is explicitly versioned so it
        # cannot overwrite the original machine-era page or confuse its receipt.
        title = f"{base_title}-human-readable-r2"
        markdown = render_phase_markdown(
            title,
            content,
            envelope["content_hash"],
            canonical,
            phase=phase,
            task_id=task_id,
            context={
                "run_id": run_id,
                "task_id": task_id,
                "input_hash": envelope.get("input_hash"),
                "approval_id": envelope.get("approval_id"),
                "approval_input_hash": envelope.get("approval_input_hash"),
                "source_revisions": envelope.get("source_revisions"),
                "source_refs": envelope.get("evidence_refs"),
            },
        )
        item_result = {
            "phase": phase,
            "task_id": task_id,
            "title": title,
            "content_hash": envelope["content_hash"],
            "markdown": markdown if dry_run else None,
        }
        result["found"].append({
            "phase": phase,
            "task_id": task_id,
            "artifact_id": item.get("artifact_id"),
            "content_hash": envelope["content_hash"],
        })
        if not dry_run:
            sync = KnowledgeSync.from_profile(
                profile_result["profile"],
                run_id=run_id,
                card_id=requirement_id,
                state_store=state,
                username=username,
                cafe_preflight=False,
                run_root_title=f"{requirement_id}-{run_id[:12]}-研发测试协作-human-readable-r2",
            )
            receipt = sync.publish_phase(
                run_id,
                {
                    "title": title,
                    "markdown": markdown,
                    "content_hash": envelope["content_hash"],
                    "canonical": canonical,
                    "phase": phase,
                    "task_id": task_id,
                    "status": "历史回填：来自已持久化 artifact",
                },
                scope="ku_only",
            )
            item_result["receipt"] = receipt
            if not receipt.get("ok"):
                result["ok"] = False
        result["published"].append(item_result)
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从已有 artifact 回填人类可读 KU 阶段文档")
    parser.add_argument("run_id")
    parser.add_argument("requirement_id")
    parser.add_argument("--project", default="bgw")
    parser.add_argument("--config-root")
    parser.add_argument("--username")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = backfill(
        args.run_id,
        args.requirement_id,
        args.project,
        config_root=args.config_root,
        username=args.username,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
