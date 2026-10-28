"""Record real iPipe responses as replayable fixtures (platform-fixture.v1).

Run by hand against the live gateway, read-only (GET only):

    python3 tests/fixtures/platform/record.py

Only structure and identity fields survive: each endpoint has a whitelist, so people,
log links, hosts and credentials never reach the repository (it is public). Fields a
controller does not read are dropped rather than redacted.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport, IpipeTransportError  # noqa: E402
from persistence_policy import redact_secrets  # noqa: E402

STAGE_KEYS = {"id", "stageName", "stageConfId", "status", "startTime", "endTime",
              "pipelineBuildId", "jobBuildBeans"}
JOB_KEYS = {"id", "jobName", "jobConfId", "jobType", "status", "startTime", "endTime",
            "stageBuildId", "skipByStage", "enableManualSkip", "realJobBuild", "realJobBuildId"}
RELEASE_KEYS = {"id", "version", "revision", "branch", "module", "releaseOutputHttpUrl",
                "releaseStatus"}
STAGE_BUILD_KEYS = {"stageBuildId", "stageName", "stageConfId", "status", "manualParams",
                    "hasDynamicParams", "pipelineBuildId", "pipelineConfId", "startTime"}
CONF_STAGE_KEYS = {"stageConfId", "stageName", "upstreamStageId", "paramBeans",
                   "inParamsMap", "groupJobs", "pipelineConfId"}
CONF_JOB_KEYS = {"jobConfId", "jobName", "jobType", "realJob"}
PARAM_KEYS = {"name", "required", "defaultValue", "isDynamic", "upStageParam", "type"}
BUILD_KEYS = {"id", "pipelineConfId", "module", "branch", "status", "startTime", "trigger",
              "stageBuilds", "name", "pipelineType"}
TRIGGER_KEYS = {"id", "module", "branch", "revision", "triggerMode", "triggerTime", "svnType"}


def _pick(value, keys):
    return {k: value[k] for k in keys if isinstance(value, dict) and k in value}


def _job(job):
    out = _pick(job, JOB_KEYS)
    real = job.get("realJobBuild")
    out["realJobBuild"] = _pick(real, RELEASE_KEYS) if isinstance(real, dict) else None
    return out


def stage_list(api, build_id):
    return [{**_pick(s, STAGE_KEYS - {"jobBuildBeans"}),
             "jobBuildBeans": [_job(j) for j in s.get("jobBuildBeans") or []]}
            for s in api.pipeline_stage_info(build_id)]


def stage_detail(api, stage_id):
    raw = api.stage_detail(stage_id)
    entities = raw.get("entities", raw)
    return {"code": raw.get("code"), "msg": raw.get("msg"), "entities": {
        "stageBuild": _pick(entities.get("stageBuild") or {}, STAGE_BUILD_KEYS),
        "realJobBuilds": [[_job(j) for j in group] for group in entities.get("realJobBuilds") or []],
    }}


def _command(job):
    shell = ((job.get("realJob") or {}).get("shellConf") or {})
    # Only the parameter references matter to the controller; the script body names hosts.
    import re
    refs = sorted(set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", shell.get("command") or "")))
    return {"shellConf": {"command": " ".join("${%s}" % r for r in refs)}} if shell else {}


def pipeline_conf(api, pipeline_id):
    conf = api.get_pipeline_by_id(pipeline_id, brief=False)
    stages = []
    for s in conf.get("stages") or []:
        item = _pick(s, CONF_STAGE_KEYS - {"paramBeans", "groupJobs", "inParamsMap"})
        item["paramBeans"] = [_pick(p, PARAM_KEYS) for p in s.get("paramBeans") or []]
        item["inParamsMap"] = {k: None for k in (s.get("inParamsMap") or {})}
        item["groupJobs"] = [[{**_pick(j, CONF_JOB_KEYS - {"realJob"}), "realJob": _command(j)}
                              for j in group] for group in s.get("groupJobs") or []]
        stages.append(item)
    return {"id": conf.get("id"), "name": conf.get("name"), "stages": stages}


def builds(api, module, revision, pipeline_id):
    out = []
    for b in api.builds_by_revision(module, revision, pipeline_id):
        item = _pick(b, BUILD_KEYS - {"trigger", "stageBuilds"})
        item["trigger"] = _pick(b.get("trigger") or {}, TRIGGER_KEYS)
        item["stageBuilds"] = [_pick(s, {"id", "stageName", "status"}) for s in b.get("stageBuilds") or []]
        out.append(item)
    return out


def error(call):
    try:
        call()
    except IpipeTransportError as e:
        return {"error": {"reason_code": e.reason_code, "status": e.status}}
    return {"error": None}


RUN = "160c6840b08842108174d08b7b6d0823"
X86 = "baidu/sysip/x86bgw"
REV = "a9715d5bc29cd61e6d4f5feb38d36061ea16da4f"
TARGETS = [
    ("bgw1995-change-275535376-stages", "pipeline_stage_info", "build/275535376",
     lambda api: stage_list(api, "275535376")),
    ("bgw1995-change-p0newcase-expression-skip", "stage_detail", "stage/649615692",
     lambda api: stage_detail(api, "649615692")),
    ("bgw1995-change-system-test-expression-skip", "stage_detail", "stage/649615694",
     lambda api: stage_detail(api, "649615694")),
    ("bgw1995-change-unit-partial-skip", "stage_detail", "stage/649615686",
     lambda api: stage_detail(api, "649615686")),
    ("bgw1995-change-p0-regression-partial-skip", "stage_detail", "stage/649615688",
     lambda api: stage_detail(api, "649615688")),
    ("bgw1995-branch-275700446-stages-final", "pipeline_stage_info", "build/275700446",
     lambda api: stage_list(api, "275700446")),
    ("bgw1995-branch-builds-by-revision", "builds_by_revision", f"revision/{REV}",
     lambda api: builds(api, X86, REV, "348101")),
    ("bgw1995-branch-regression-detail", "stage_detail", "stage/649960670",
     lambda api: stage_detail(api, "649960670")),
    ("bgw1995-branch-config-deleted-stage-detail", "stage_detail", "stage/649960671",
     lambda api: stage_detail(api, "649960671")),
    ("bgw1995-branch-release-detail", "stage_detail", "stage/649960675",
     lambda api: stage_detail(api, "649960675")),
    ("bgw1995-branch-pipeline-conf-348101", "get_pipeline_by_id", "pipeline/348101",
     lambda api: pipeline_conf(api, "348101")),
    ("gateway-get-release-info-404", "release_info", f"release_info/{X86}",
     lambda api: error(lambda: api.release_info(X86, "x86bgw-dpdk-2011-syncookie"))),
]


def main():
    api = IpipeApiClient(IpipeHttpTransport(), current_user=sys.argv[1] if len(sys.argv) > 1 else "zhaotao02")
    for fixture_id, endpoint, source, read in TARGETS:
        response = redact_secrets(read(api))
        body = json.dumps(response, ensure_ascii=False, sort_keys=True)
        record = {
            "schema": "platform-fixture.v1", "fixture_id": fixture_id, "origin": "RECORDED",
            "source_run_id": RUN, "source_object": source, "platform": "ipipe",
            "endpoint": endpoint, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "redaction": "endpoint whitelist (record.py) + redact_secrets",
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "derived_from": None, "response": response,
        }
        (HERE / f"{fixture_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(fixture_id, len(body))


if __name__ == "__main__":
    main()
