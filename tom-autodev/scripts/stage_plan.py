"""Stage plan snapshots for manual BranchPipeline stages (MVP contract 3.3).

A G8 for a manual stage binds two snapshots, so an approval granted against one shape of
the pipeline cannot run against another:

- the build's own stage list (which stages exist and in what order -- the build keeps
  the shape it started with even when the configuration is edited later), and
- the live configuration of the stages, reduced to what a stage run consumes: identity,
  order, declared parameters, job types.

Both are canonical JSON hashed with sha256, so the same pipeline always yields the same
hash and any change to a declared parameter, a job or the stage order yields another.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA = "stage-plan.v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def config_snapshot(configuration: dict[str, Any]) -> list[dict[str, Any]]:
    """The parts of a pipeline configuration a manual stage run depends on."""
    stages = []
    for stage in configuration.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        stages.append({
            "stageConfId": str(stage.get("stageConfId") or ""),
            "stageName": stage.get("stageName"),
            "upstreamStageId": str(stage.get("upstreamStageId") or ""),
            "paramBeans": sorted(
                ({"name": p.get("name"), "required": bool(p.get("required"))}
                 for p in stage.get("paramBeans") or [] if isinstance(p, dict)),
                key=lambda p: str(p["name"])),
            "inParamsMap": sorted(str(k) for k in (stage.get("inParamsMap") or {})),
            "jobs": sorted(
                ({"jobConfId": str(j.get("jobConfId") or ""), "jobType": j.get("jobType")}
                 for group in stage.get("groupJobs") or [] for j in group
                 if isinstance(j, dict)),
                key=lambda j: j["jobConfId"]),
        })
    return sorted(stages, key=lambda s: s["stageConfId"])


def config_hash(configuration: dict[str, Any]) -> str:
    return _hash({"schema": SCHEMA, "stages": config_snapshot(configuration)})


def build_snapshot(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stage identity and order of one build (normalized stages)."""
    return [{"stage_build_id": s.get("stage_build_id"), "stage_conf_id": s.get("stage_conf_id"),
             "name": s.get("name")} for s in stages]


def build_hash(stages: list[dict[str, Any]]) -> str:
    return _hash({"schema": SCHEMA, "build": build_snapshot(stages)})
